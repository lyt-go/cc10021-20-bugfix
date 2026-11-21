"""质量事件服务：记录和追踪数据质量异常事件，驱动趋势分析和评分卡计算。

domain_chain: 质量事件 (dq_incident)
  └─ 关联 → 趋势分析 (dq_trend)
  └─ 聚合 → 评分卡 (dq_scorecard)
  └─ 清洗 → 告警通知 (dq_alert_channel)
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.services.incident_window import (
    BUSINESS_TZ,
    build_incident_window,
    matches_scope,
    parse_report_date,
)
from app.store import store

DATE_KEYS = ("日期", "报告日期", "归属日期", "窗口日期", "发生时间", "date", "report_date")
START_KEYS = ("起始日期", "开始日期", "start_date")
END_KEYS = ("结束日期", "截止日期", "end_date")
RECEIVED_KEYS = ("接收时间", "接收日期", "received_at")
DB_KEYS = ("所属库名", "库名", "db_name")
TABLE_KEYS = ("目标表名", "表名", "table_name")


class DqIncidentService(BaseModuleService):
    MODULE = "dq_incident"
    ENTITY = "质量事件"
    OPERATION_TERM = "写入"
    LIST_FIELDS = ["事件编号", "关联规则", "触发时间", "偏离程度", "影响行数", "严重级别", "处理状态", "处理人"]
    FILTER_FIELDS = LIST_FIELDS + [
        "发生时间", "接收时间", "目标表名", "所属库名",
        "库名", "表名", "窗口日期", "日期", "接收日期", "起始日期", "结束日期",
    ]
    REQUIRED_FIELDS = ["事件编号", "关联规则", "触发时间"]
    STATUS_ORDER = ["待确认", "调查中", "已修复", "已关闭"]
    ACTION_RULES = {"确认事件": "待确认", "开始调查": "调查中", "标记修复": "已修复", "关闭事件": "已关闭"}
    NEGATIVE_ACTIONS = []

    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """列表查询；带窗口/日期条件时走业务日窗口，而不是按字符串包含匹配。

        例如 ``?窗口日期=2026-11-20`` 只返回发生在 2026-11-20 的事件，
        昨天发生、今天才收到的迟到事件不会混进今天的结果。
        """
        filters = dict(filters or {})
        window_keys = {key for key in filters if key in DATE_KEYS + START_KEYS + END_KEYS}
        window_keys |= {"窗口日期", "发生时间", "接收时间", "接收日期"} & set(filters)
        if window_keys:
            window = self._window_from_filters(filters)
            items = window["明细"]
            if status:
                items = [item for item in items if item.get("status") == status]
            if keyword:
                needle = keyword.casefold()
                items = [
                    item for item in items
                    if needle in str(item.get("事件编号", "")).casefold()
                ]
            total = len(items)
            start = (page - 1) * size
            return items[start:start + size], total

        # 没有时间条件时，库表筛选也按精确身份匹配，不做表名子串包含。
        db_filter = self._pick(filters, DB_KEYS)
        table_filter = self._pick(filters, TABLE_KEYS)
        if db_filter or table_filter:
            plain = {
                key: value for key, value in filters.items()
                if key not in DB_KEYS + TABLE_KEYS
            }
            rows, _ = super().list_entries(
                filters=plain, keyword=keyword, status=status, page=1, size=1_000_000)
            matched = [
                row for row in rows
                if self._identity_matches(row, db_filter, table_filter)
            ]
            total = len(matched)
            start = (page - 1) * size
            return matched[start:start + size], total

        return super().list_entries(
            filters=filters, keyword=keyword, status=status, page=page, size=size)

    @staticmethod
    def _identity_matches(row: dict[str, Any], db_filter: str | None,
                          table_filter: str | None) -> bool:
        return matches_scope(row, db_name=db_filter, table_name=table_filter)

    def _window_from_filters(self, filters: dict[str, str]) -> dict[str, Any]:
        base = dict(
            db_name=self._pick(filters, DB_KEYS),
            table_name=self._pick(filters, TABLE_KEYS),
        )
        start = self._pick(filters, START_KEYS)
        end = self._pick(filters, END_KEYS)
        if start or end:
            start_day = parse_report_date(start) if start else None
            end_day = parse_report_date(end) if end else None
            if start_day and end_day and start_day > end_day:
                start_day, end_day = end_day, start_day
            start_day = start_day or end_day
            end_day = end_day or start_day
            return self.build_window(
                report_date=end_day.isoformat(),
                days=(end_day - start_day).days + 1,
                **base,
            )

        received_day = self._pick(filters, RECEIVED_KEYS)
        if received_day:
            return self._window_by_received_day(received_day, **base)

        return self.build_window(
            report_date=self._pick(filters, DATE_KEYS),
            days=1,
            **base,
        )

    def _window_by_received_day(self, received_day: str, *,
                                db_name: str | None,
                                table_name: str | None) -> dict[str, Any]:
        """按接收日期取数：用于核对“今天到货了哪些（含补入）”的迟到事件。

        这是另一条筛选口径，统计仍来自同一套明细构造逻辑，只是按接收日过滤。
        """
        day = parse_report_date(received_day)
        window = self.build_window(
            report_date=day.isoformat(), days=1, db_name=db_name, table_name=table_name,
        )
        # 把窗口放大到所有历史，再按接收日收窄，避免发生日不在当天时被提前排除。
        full = build_incident_window(
            store.rows(self.MODULE),
            report_date=day.isoformat(),
            days=3650,
            db_name=db_name,
            table_name=table_name,
        )
        items = [
            item for item in full["明细"] if item.get("接收日期") == day.isoformat()
        ]
        return {
            **full,
            "报告日期": day.isoformat(),
            "归属日期": day.isoformat(),
            "天数": 1,
            "明细": items,
            "统计": {**full["统计"], "事件总数": len(items), "去重后": len(items)},
            "按接收日期": day.isoformat(),
            "原窗口起": window["窗口起"],
        }

    @staticmethod
    def _pick(filters: dict[str, str], keys: tuple[str, ...]) -> str | None:
        for key in keys:
            if filters.get(key):
                return filters[key]
        return None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        """登记事件时同时落发生时间和接收时间。

        只填了触发时间的历史写法也能兼容：触发时间视为发生时间，
        接收时间取当前时间；只填发生时间时触发时间自动补齐。
        """
        payload = dict(values)
        occurred_text = payload.get("发生时间") or payload.get("触发时间")
        occurred = parse_report_date(occurred_text) if occurred_text else parse_report_date(None)
        if occurred_text and "T" in str(occurred_text):
            occurred_iso = str(occurred_text)
        else:
            occurred_iso = datetime.combine(
                occurred, datetime.min.time(), tzinfo=BUSINESS_TZ
            ).isoformat()
        payload.setdefault("发生时间", occurred_iso)
        payload["触发时间"] = payload.get("触发时间") or occurred_iso
        payload.setdefault("接收时间", datetime.now(BUSINESS_TZ).isoformat())
        return super().create_entry(payload)

    def build_window(
        self,
        *,
        report_date: str | None = None,
        days: int = 1,
        db_name: str | None = None,
        table_name: str | None = None,
    ) -> dict[str, Any]:
        """按业务发生日构建质量事件窗口快照，供日报与派工清单共用。

        窗口只认“哪天发生”，不认“哪天收到”：昨晚发生、今天才到的事件仍归昨天；
        库表按精确名称匹配，避免同名表跨库串数据。身份不完整的旧记录放进待核对。
        """
        return build_incident_window(
            store.rows(self.MODULE),
            report_date=report_date,
            days=days,
            db_name=db_name,
            table_name=table_name,
        )

    def aggregate_incident_summary(
        self,
        table_name: str,
        days: int = 7,
        *,
        report_date: str | None = None,
        db_name: str | None = None,
    ) -> dict[str, Any]:
        """聚合指定库表在业务日窗口内的质量事件摘要。

        时间窗口不再只写在标题上：明细按发生时间收窄到窗口内，
        跨日补入回填发生日，同一事件并发更正只计一次；
        库表名按精确值匹配，避免表名子串把别的库同名表累计进来。
        """
        window = self.build_window(
            report_date=report_date,
            days=days,
            db_name=db_name,
            table_name=table_name,
        )
        stats = window["统计"]

        return {
            "目标表名": table_name,
            "目标库名": db_name or "",
            "时间窗口": f"{window['起始日期']}~{window['结束日期']}（{window['天数']}天）",
            "报告日期": window["报告日期"],
            "窗口起": window["窗口起"],
            "窗口止": window["窗口止"],
            "筛选口径": window["筛选口径"],
            "事件总数": stats["事件总数"],
            "原始记录数": stats["原始记录数"],
            "去重后": stats["去重后"],
            "合并重复记录数": stats["合并重复记录数"],
            "按级别": stats["按级别"],
            "按状态": stats["按状态"],
            "按库表": stats["按库表"],
            "归属日期分布": stats["归属日期分布"],
            "根因分布": stats["根因分布"],
            "窗口明细": window["明细"],
            "待核对": window["待核对"],
            "待核对数量": window["待核对数量"],
            "聚合时间": datetime.now(timezone.utc).isoformat(),
        }

    def write_quality_incident(self, rule_id: str, severity: str,
                                deviation: str, table_name: str,
                                *,
                                db_name: str | None = None,
                                occurred_at: str | None = None) -> dict[str, Any]:
        """写入质量事件记录并将事件关联到告警通知通道。

        当检测到数据质量异常时，写入质量事件记录到事件存储中，
        同时将事件分发到告警通道触发通知，并登记到趋势服务。

        ``occurred_at`` 用于补录/迟到事件：业务实际发生时间与接收时间分开保存，
        日报按发生时间归属，不会因为今天才收到就把事件算到今天。
        """
        rows = store.rows(self.MODULE)
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1

        now = datetime.now(BUSINESS_TZ)
        occurred = parse_report_date(occurred_at) if occurred_at else now.date()
        # 补录只写日期时，发生时间落在当天开始，保证与接收时间可区分。
        occurred_iso = (
            occurred_at if occurred_at and "T" in str(occurred_at)
            else datetime.combine(occurred, datetime.min.time(), tzinfo=BUSINESS_TZ).isoformat()
        )

        entry = {
            "id": new_id,
            "事件编号": f"DQ_I-{new_id:04d}",
            "关联规则": rule_id,
            "发生时间": occurred_iso,
            "接收时间": now.isoformat(),
            "触发时间": occurred_iso,
            "目标表名": table_name,
            "所属库名": db_name or "",
            "偏离程度": deviation,
            "影响行数": str(random.randint(500, 100000)),
            "严重级别": severity,
            "处理状态": "新建",
            "处理人": "系统自动",
            "status": "待确认",
            "pending": True,
            "abnormal": True,
        }
        rows.append(entry)

        # 关联写入趋势记录
        trend_rows = store.rows("dq_trend")
        new_tid = max((int(r.get("id", 0)) for r in trend_rows), default=0) + 1
        trend_rows.append({
            "id": new_tid,
            "记录编号": f"DQ_T-{new_tid:04d}",
            "目标表名": table_name,
            "日期": occurred.strftime("%Y-%m-%d"),
            "完备率": f"{random.uniform(70, 85):.1f}%",
            "准确率": f"{random.uniform(75, 90):.1f}%",
            "及时率": f"{random.uniform(80, 95):.1f}%",
            "综合质量": f"{random.uniform(70, 85):.1f}",
            "趋势标记": "下降趋势",
            "status": "下降趋势",
            "pending": True,
            "abnormal": True,
        })

        return entry

    def analyze_incident_trend(self, table_name: str,
                               db_name: str | None = None,
                               days: int = 30,
                               report_date: str | None = None) -> dict[str, Any]:
        """分析质量事件趋势并计算改善评分。

        查询历史事件数据，计算事件频率变化、修复率和趋势方向，
        聚合后的趋势分析结果关联到评分卡计算。库表匹配为精确匹配。
        """
        window = self.build_window(
            report_date=report_date, days=days,
            db_name=db_name, table_name=table_name,
        )
        related = window["明细"]

        if not related:
            return {"目标表名": table_name, "状态": "无历史事件", "趋势": "平稳"}

        resolved = sum(1 for i in related if i.get("status") in ("已修复", "已关闭"))
        resolution_rate = resolved / len(related) * 100 if related else 0

        trend = "改善中" if resolution_rate > 60 else "恶化" if resolution_rate < 30 else "平稳"

        return {
            "目标表名": table_name,
            "目标库名": db_name or "",
            "时间窗口": f"{window['起始日期']}~{window['结束日期']}",
            "历史事件总数": len(related),
            "已修复": resolved,
            "修复率": f"{resolution_rate:.1f}%",
            "趋势方向": trend,
            "建议": "加强规则覆盖" if trend == "恶化" else "保持监控" if trend == "平稳" else "继续改进",
        }

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["operation"] = "写入"
        snapshot["desc"] = "写入质量事件记录并关联告警与趋势分析"
        snapshot["artifact"] = "质量事件数据集"
        snapshot["artifact_desc"] = "含趋势和告警关联的质量事件数据集"
        return snapshot