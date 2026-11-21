"""质量报告服务：聚合巡检和质量事件数据，生成多维度质量分析报告。

domain_chain: 质量报告 (dq_report)
  └─ 聚合 → 质量趋势 (dq_trend)
  └─ 导出 → 告警通知 (dq_alert_channel)

日报取数以“质量事件窗口快照”为唯一事实来源：
1. ``aggregate_quality_report`` 先按业务发生日构建窗口快照并落库；
2. 报告记录、派工清单、事件详情都引用同一个 ``窗口快照编号``；
3. 所有统计数字从该快照读取，保证报告入口与事件详情可以逐条核对归属日期。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.dq_incident import DqIncidentService
from app.services.incident_window import BUSINESS_TZ
from app.store import store

SNAPSHOT_MODULE = "dq_report_snapshot"
DISPATCH_MODULE = "dq_report_dispatch"


class DqReportService(BaseModuleService):
    MODULE = "dq_report"
    ENTITY = "质量报告"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["报告编号", "报告周期", "覆盖表数", "通过规则数", "失败规则数", "趋势分析", "生成时间", "报告版本"]
    # 日报记录额外可按归属日期、窗口快照编号筛选，便于回查某天的取数快照。
    FILTER_FIELDS = LIST_FIELDS + ["归属日期", "窗口快照编号", "报告日期"]
    REQUIRED_FIELDS = ["报告编号", "报告周期", "覆盖表数"]
    STATUS_ORDER = ["草稿", "生成中", "已完成", "已归档"]
    ACTION_RULES = {"生成报告": "生成中", "确认完成": "已完成", "归档报告": "已归档"}
    NEGATIVE_ACTIONS = []

    def aggregate_quality_report(self, period: str = "daily",
                                 report_date: str | None = None,
                                 *,
                                 days: int | None = None,
                                 db_name: str | None = None,
                                 table_name: str | None = None) -> dict[str, Any]:
        """聚合全量巡检结果和质量事件，生成多维度质量分析报告。

        查询巡检执行记录、质量事件、评分卡和趋势数据，聚合各维度指标，
        写入质量报告记录、窗口快照和派工清单，并导出趋势快照。

        ``report_date`` 缺省取当天；``days`` 缺省时日报按 1 天、周报按 7 天。
        质量事件相关统计全部来自同一份窗口快照，跨日补入事件归属发生当天。
        """
        incident_service = DqIncidentService()
        window_days = days
        if window_days is None:
            window_days = 7 if period in ("weekly", "周报", "week") else 1

        window = incident_service.build_window(
            report_date=report_date,
            days=window_days,
            db_name=db_name,
            table_name=table_name,
        )
        stats = window["统计"]
        details = window["明细"]

        # 巡检/评分卡沿用原口径，只参与报告其他维度。
        check_rows = store.rows("dq_check_run")
        scorecard_rows = store.rows("dq_scorecard")
        trend_rows = store.rows("dq_trend")

        total_checks = len(check_rows)
        passed_checks = sum(1 for r in check_rows if not r.get("abnormal"))
        failed_checks = total_checks - passed_checks

        # 质量事件统计只从窗口快照派生，保证可逐条核对。
        total_incidents = stats["事件总数"]
        open_incidents = sum(
            1 for r in details if r.get("status") in ("待确认", "调查中")
        )
        resolved_incidents = sum(
            1 for r in details if r.get("status") in ("已修复", "已关闭")
        )

        quality_values = []
        for t in trend_rows:
            try:
                quality_values.append(float(t.get("综合质量", "0")))
            except (ValueError, TypeError):
                pass
        avg_quality = sum(quality_values) / len(quality_values) if quality_values else 0

        # 1) 先落窗口快照，报告与派工都引用它。
        snapshot = self._save_window_snapshot(
            window, period=period, db_name=db_name, table_name=table_name,
        )

        # 2) 写报告记录，并绑定窗口快照编号。
        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        report = {
            "id": new_id,
            "报告编号": f"DQ_R-{new_id:04d}",
            "报告周期": period,
            "覆盖表数": str(len({item["表名"] for item in details if item["表名"]}) or len(scorecard_rows)),
            "通过规则数": str(passed_checks),
            "失败规则数": str(failed_checks),
            "趋势分析": "上升" if avg_quality >= 85 else "平稳" if avg_quality >= 70 else "下降",
            "生成时间": datetime.now(timezone.utc).isoformat(),
            "报告版本": "v1.0",
            "窗口快照编号": snapshot["快照编号"],
            "归属日期": window["归属日期"],
            "窗口起": window["窗口起"],
            "窗口止": window["窗口止"],
            "筛选口径": window["筛选口径"],
            "事件总数": str(total_incidents),
            "待核对数量": str(window["待核对数量"]),
            "status": "已完成",
            "pending": False,
            "abnormal": failed_checks > passed_checks,
        }
        rows.append(report)

        snapshot["报告编号"] = report["报告编号"]

        # 3) 把筛选结果回写派工清单：窗口内每条确认事件一条待领取派工单。
        dispatch_items = self._write_dispatch_list(report, snapshot, details)
        report["派工清单数"] = str(len(dispatch_items))
        report["派工清单编号"] = [item["派工单号"] for item in dispatch_items]

        dim_scores = self._compute_dimension_scores(scorecard_rows)

        return {
            "报告编号": report["报告编号"],
            "报告周期": period,
            "报告日期": window["报告日期"],
            "归属日期": window["归属日期"],
            "时间窗口": f"{window['起始日期']}~{window['结束日期']}",
            "窗口起": window["窗口起"],
            "窗口止": window["窗口止"],
            "窗口快照编号": snapshot["快照编号"],
            "筛选口径": window["筛选口径"],
            "巡检总次数": total_checks,
            "巡检通过率": f"{passed_checks / total_checks * 100:.1f}%" if total_checks > 0 else "N/A",
            "质量事件总数": total_incidents,
            "原始记录数": stats["原始记录数"],
            "合并重复记录数": stats["合并重复记录数"],
            "未处理事件": open_incidents,
            "已修复事件": resolved_incidents,
            "修复率": f"{resolved_incidents / total_incidents * 100:.1f}%" if total_incidents > 0 else "N/A",
            "按库表": stats["按库表"],
            "归属日期分布": stats["归属日期分布"],
            "平均综合质量": round(avg_quality, 2),
            "维度得分": dim_scores,
            "窗口明细": details,
            "待核对": window["待核对"],
            "待核对数量": window["待核对数量"],
            "派工清单": dispatch_items,
            "派工清单数": len(dispatch_items),
            "生成时间": report["生成时间"],
            "趋势": report["趋势分析"],
        }

    def _save_window_snapshot(self, window: dict[str, Any], *, period: str,
                              db_name: str | None,
                              table_name: str | None) -> dict[str, Any]:
        """持久化窗口快照：报告入口、事件详情和派工清单共用这一份明细。"""
        snapshots = store.rows(SNAPSHOT_MODULE)
        new_sid = max((int(r.get("id", 0)) for r in snapshots), default=0) + 1
        snapshot = {
            "id": new_sid,
            "快照编号": f"DQ_W-{new_sid:04d}",
            "报告周期": period,
            "报告日期": window["报告日期"],
            "归属日期": window["归属日期"],
            "起始日期": window["起始日期"],
            "结束日期": window["结束日期"],
            "天数": window["天数"],
            "窗口起": window["窗口起"],
            "窗口止": window["窗口止"],
            "库表范围": {
                "库名": db_name or "",
                "表名": table_name or "",
            },
            "筛选口径": window["筛选口径"],
            "明细": window["明细"],
            "统计": window["统计"],
            "待核对": window["待核对"],
            "待核对数量": window["待核对数量"],
            "生成时间": window["生成时间"],
            "报告编号": "",
            "status": "已快照",
            "pending": False,
            "abnormal": bool(window["待核对数量"]),
        }
        snapshots.append(snapshot)
        return snapshot

    def _write_dispatch_list(self, report: dict[str, Any], snapshot: dict[str, Any],
                             details: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把窗口明细回写成值班派工清单，每条事件只派一次。"""
        dispatches = store.rows(DISPATCH_MODULE)
        created: list[dict[str, Any]] = []
        for item in details:
            new_did = max((int(r.get("id", 0)) for r in dispatches), default=0) + 1
            dispatch = {
                "id": new_did,
                "派工单号": f"DQ_D-{new_did:04d}",
                "报告编号": report["报告编号"],
                "窗口快照编号": snapshot["快照编号"],
                "事件编号": item["事件编号"],
                "源记录id": item["源记录id"],
                "库名": item["库名"],
                "表名": item["表名"],
                "归属日期": item["归属日期"],
                "发生时间": item["发生时间"],
                "接收时间": item["接收时间"],
                "是否跨日补入": item["是否跨日补入"],
                "严重级别": item["严重级别"],
                "处理状态": item["处理状态"],
                "值班人": "数据工程组",
                "领取状态": "待领取",
                "status": "待领取",
                "pending": True,
                "abnormal": item["严重级别"] in ("critical", "warning"),
            }
            dispatches.append(dispatch)
            created.append(dispatch)
        return created

    def get_window_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        """按快照编号读取窗口明细，供报告入口/事件详情核对归属日期。"""
        for snapshot in store.rows(SNAPSHOT_MODULE):
            if int(snapshot.get("id", 0)) == int(snapshot_id):
                return snapshot
        return None

    def find_window_snapshot(self, reference: str | int) -> dict[str, Any] | None:
        """支持按快照编号（DQ_W-xxxx）、报告编号（DQ_R-xxxx）或报告主键查快照。"""
        text = str(reference).strip()
        if text.isdigit():
            snapshot = self.get_window_snapshot(int(text))
            if snapshot is not None:
                return snapshot
            report = store.find(self.MODULE, int(text))
            if report is not None:
                text = str(report.get("窗口快照编号") or report.get("报告编号") or "")
        for snapshot in store.rows(SNAPSHOT_MODULE):
            if text and text in (snapshot.get("快照编号"), snapshot.get("报告编号")):
                return snapshot
        return None

    def get_dispatch_list(self, report_reference: str | int) -> list[dict[str, Any]]:
        """按报告编号或快照编号读取派工清单。"""
        text = str(report_reference).strip()
        snapshot = self.find_window_snapshot(text)
        report_no = text
        snapshot_no = ""
        if snapshot is not None:
            report_no = snapshot.get("报告编号") or report_no
            snapshot_no = snapshot.get("快照编号") or ""
        return [
            row for row in store.rows(DISPATCH_MODULE)
            if row.get("报告编号") == report_no
            or (snapshot_no and row.get("窗口快照编号") == snapshot_no)
        ]

    def claim_dispatch(self, report_reference: str | int,
                       claimer: str = "数据工程组") -> dict[str, Any]:
        """领取某份日报的派工清单，返回领取到的事件及其归属日期。"""
        items = self.get_dispatch_list(report_reference)
        snapshot = self.find_window_snapshot(report_reference)
        claimed_dates: dict[str, int] = {}
        for item in items:
            if item.get("领取状态") == "待领取":
                item["领取状态"] = "已领取"
                item["status"] = "已领取"
                item["pending"] = False
                item["领取人"] = claimer
                item["领取时间"] = datetime.now(BUSINESS_TZ).isoformat()
            day = str(item.get("归属日期") or "")
            claimed_dates[day] = claimed_dates.get(day, 0) + 1
        return {
            "报告编号": snapshot.get("报告编号") if snapshot else str(report_reference),
            "窗口快照编号": snapshot.get("快照编号") if snapshot else "",
            "报告日期": snapshot.get("报告日期") if snapshot else "",
            "领取人": claimer,
            "领取事件数": len(items),
            "按归属日期": claimed_dates,
            "派工清单": items,
        }

    def _compute_dimension_scores(self, scorecard_rows: list[dict]) -> dict[str, float]:
        """计算质量维度得分汇总：完整性、准确性、时效性。"""
        dims = {"完整性": [], "准确性": [], "时效性": []}
        for s in scorecard_rows:
            for dim_key, field in [("完整性", "完整性得分"), ("准确性", "准确性得分"), ("时效性", "时效性得分")]:
                try:
                    val = float(s.get(field, "0"))
                    dims[dim_key].append(val)
                except (ValueError, TypeError):
                    pass
        return {
            k: round(sum(v) / len(v), 1) if v else 0.0
            for k, v in dims.items()
        }

    def export_report_csv(self, report_id: int) -> str | None:
        """导出质量报告为CSV格式，包含所有维度数据和趋势标记。"""
        report = store.find(self.MODULE, report_id)
        if not report:
            return None

        lines = [
            "报告编号,报告周期,报告日期,覆盖表数,通过规则数,失败规则数,趋势,生成时间,版本,窗口快照编号",
            f"{report.get('报告编号')},{report.get('报告周期')},{report.get('归属日期')},{report.get('覆盖表数')},{report.get('通过规则数')},{report.get('失败规则数')},{report.get('趋势分析')},{report.get('生成时间')},{report.get('报告版本')},{report.get('窗口快照编号')}",
        ]
        return "\n".join(lines)

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "聚合"
        snapshot["desc"] = "按业务发生日聚合质量事件窗口并生成质量分析报告"
        snapshot["artifact"] = "质量报告数据集"
        snapshot["artifact_desc"] = "含窗口快照和派工清单的质量报告数据集"
        return snapshot
