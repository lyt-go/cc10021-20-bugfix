"""质量事件窗口 / 质量日报回归测试。

覆盖的场景：
1. 跨日补入：昨晚发生、今天收到的事件只归发生当天；
2. 并发更正：同一事件两条记录在窗口内只计一次；
3. 同名表跨库：按库精确筛选，表名子串不再串表；
4. 身份不完整的旧记录进入待核对，不混入确认统计；
5. 报告入口、事件详情、派工清单共用同一份窗口快照。
"""
from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.routers import quality_daily
from app.services.dq_incident import DqIncidentService
from app.services.dq_report import DqReportService
from app.store import store


@pytest.fixture(autouse=True)
def clean_store():
    """每个用例使用一份独立数据，避免种子数据和其他用例互相污染。"""
    backup = {name: copy.deepcopy(rows) for name, rows in store._tables.items()}
    for module in ("dq_incident", "dq_report", "dq_report_snapshot", "dq_report_dispatch"):
        store._tables[module] = []
    yield
    store._tables = backup


def _seed_pair(*, include_archive: bool = True) -> None:
    """铺一份最小事件集：跨日补入的订单事件（含并发更正）+ 其他库同名表。"""
    store.rows("dq_incident").extend([
        {
            "id": 4,
            "事件编号": "DQ_I-0004",
            "关联规则": "DQ_R-ORDER-001",
            "触发时间": "2026-11-20T22:30:00+08:00",
            "发生时间": "2026-11-20T22:30:00+08:00",
            "接收时间": "2026-11-21T08:05:00+08:00",
            "目标表名": "ods_order_info",
            "所属库名": "order_db",
            "严重级别": "critical",
            "处理状态": "新建",
            "status": "待确认",
            "pending": True,
            "abnormal": True,
        },
        {
            "id": 5,
            "事件编号": "DQ_I-0004",
            "关联规则": "DQ_R-ORDER-001",
            "触发时间": "2026-11-20T22:30:00+08:00",
            "发生时间": "2026-11-20T22:30:00+08:00",
            "接收时间": "2026-11-21T08:07:00+08:00",
            "目标表名": "ods_order_info",
            "所属库名": "order_db",
            "严重级别": "critical",
            "处理状态": "更正",
            "status": "调查中",
            "pending": True,
            "abnormal": True,
        },
    ])
    if include_archive:
        store.rows("dq_incident").append({
            "id": 6,
            "事件编号": "DQ_I-0006",
            "关联规则": "DQ_R-ORDER-002",
            "触发时间": "2026-11-21T01:10:00+08:00",
            "发生时间": "2026-11-21T01:10:00+08:00",
            "接收时间": "2026-11-21T01:12:00+08:00",
            "目标表名": "ods_order_info",
            "所属库名": "archive_db",
            "严重级别": "warning",
            "处理状态": "新建",
            "status": "待确认",
            "pending": True,
            "abnormal": True,
        })


def _row(number: str, *, occurred: str, received: str, db: str, table: str,
         level: str = "warning", status: str = "待确认") -> dict:
    return {
        "事件编号": number,
        "关联规则": "DQ_R-TEST",
        "发生时间": occurred,
        "接收时间": received,
        "触发时间": occurred,
        "所属库名": db,
        "目标表名": table,
        "严重级别": level,
        "处理状态": "新建",
        "status": status,
        "pending": True,
        "abnormal": True,
    }


class TestIncidentWindow:
    def test_late_record_belongs_to_occurrence_day(self):
        svc = DqIncidentService()
        _seed_pair(include_archive=False)

        yesterday = svc.aggregate_incident_summary(
            "ods_order_info", days=1, report_date="2026-11-20", db_name="order_db")
        today = svc.aggregate_incident_summary(
            "ods_order_info", days=1, report_date="2026-11-21", db_name="order_db")

        assert yesterday["事件总数"] == 1
        assert [item["事件编号"] for item in yesterday["窗口明细"]] == ["DQ_I-0004"]
        assert today["事件总数"] == 0
        # 窗口不再只在标题上：明细时间确实落在窗口范围内。
        assert yesterday["窗口起"].startswith("2026-11-20T00:00")
        assert yesterday["窗口止"].startswith("2026-11-21T00:00")

    def test_concurrent_correction_counted_once(self):
        svc = DqIncidentService()
        _seed_pair(include_archive=False)

        window = svc.build_window(
            report_date="2026-11-20", days=1,
            db_name="order_db", table_name="ods_order_info")
        assert window["统计"]["事件总数"] == 1
        assert window["统计"]["原始记录数"] == 2
        assert window["统计"]["合并重复记录数"] == 1
        winner = window["明细"][0]
        # 保留最新接收的那条更正记录，并留下被合并的源记录 id 供核对。
        assert winner["接收时间"].startswith("2026-11-21T08:07")
        assert winner["合并记录id"] == [4]

    def test_same_table_name_in_other_db_is_not_mixed(self):
        svc = DqIncidentService()
        _seed_pair()

        order = svc.aggregate_incident_summary(
            "ods_order_info", days=1, report_date="2026-11-21", db_name="order_db")
        all_db = svc.aggregate_incident_summary(
            "ods_order_info", days=1, report_date="2026-11-21")

        assert order["事件总数"] == 0
        assert all_db["事件总数"] == 1
        assert all_db["按库表"] == {"archive_db.ods_order_info": 1}

    def test_table_name_is_not_substring_matched(self):
        svc = DqIncidentService()
        store.rows("dq_incident").append(_row(
            "DQ_I-9005",
            occurred="2026-11-21T02:00:00+08:00",
            received="2026-11-21T02:01:00+08:00",
            db="order_db", table="ods_order_info",
        ))
        # “order” 只是 ods_order_info 的子串，旧实现会把它算进来。
        summary = svc.aggregate_incident_summary(
            "order", days=1, report_date="2026-11-21")
        assert summary["事件总数"] == 0

    def test_incomplete_identity_goes_to_manual_check(self):
        svc = DqIncidentService()
        store.rows("dq_incident").append({
            "事件编号": "DQ_I-9006",
            "关联规则": "质量事件管理样例旧记录",
            "触发时间": "2026-11-21T03:00:00+08:00",
            "严重级别": "info",
            "status": "待确认",
        })

        window = svc.build_window(report_date="2026-11-21", days=1)
        assert window["统计"]["事件总数"] == 0
        assert window["待核对数量"] == 1
        assert window["待核对"][0]["原因"] == "库表身份缺失"
        assert "DQ_I-9006" not in [item["事件编号"] for item in window["明细"]]

    def test_no_time_legacy_only_listed_for_today(self):
        svc = DqIncidentService()
        store.rows("dq_incident").append({
            "事件编号": "DQ_I-9008",
            "关联规则": "质量事件管理样例无时间记录",
            "严重级别": "info",
            "status": "待确认",
        })
        today = DqIncidentService().build_window(days=1)["报告日期"]
        assert svc.build_window(report_date=today, days=1)["待核对数量"] == 1
        # 历史日报不再反复列出同一条无时间旧记录。
        assert svc.build_window(report_date="2026-11-20", days=1)["待核对数量"] == 0

    def test_missing_db_is_manual_when_scope_is_db(self):
        svc = DqIncidentService()
        store.rows("dq_incident").append({
            "事件编号": "DQ_I-9009",
            "关联规则": "DQ_R-NODB",
            "触发时间": "2026-11-20T10:00:00+08:00",
            "目标表名": "ods_order_info",
            "严重级别": "info",
            "status": "待确认",
        })
        window = svc.build_window(
            report_date="2026-11-20", days=1,
            db_name="order_db", table_name="ods_order_info")
        assert window["统计"]["事件总数"] == 0
        assert window["待核对数量"] == 1
        assert window["待核对"][0]["原因"] == "库名缺失"

    def test_legacy_trigger_time_is_supported(self):
        svc = DqIncidentService()
        # 历史记录的库表身份写在关联规则前缀里，时间只有触发时间。
        store.rows("dq_incident").append({
            "事件编号": "DQ_I-9007",
            "关联规则": "trend:report_db.ods_order_info",
            "触发时间": "2026-11-21 09:00:00",
            "严重级别": "info",
            "status": "待确认",
        })
        window = svc.build_window(
            report_date="2026-11-21", days=1,
            db_name="report_db", table_name="ods_order_info")
        assert window["统计"]["事件总数"] == 1
        item = window["明细"][0]
        assert item["发生时间来源"] == "触发时间"
        assert item["库名"] == "report_db"
        assert item["表名"] == "ods_order_info"


class TestDailyReportSnapshot:
    def test_report_stats_come_from_window_snapshot(self):
        _seed_pair()
        svc = DqReportService()
        report = svc.aggregate_quality_report("daily", "2026-11-20")

        snapshot = svc.find_window_snapshot(report["报告编号"])
        assert snapshot is not None
        assert snapshot["快照编号"] == report["窗口快照编号"]
        assert snapshot["统计"]["事件总数"] == report["质量事件总数"]
        assert snapshot["明细"] == report["窗口明细"]

        # 报告里的事件数与单独跑窗口得到的结果一致，可逐条核对归属日期。
        incident_window = DqIncidentService().build_window(
            report_date="2026-11-20", days=1)
        assert report["质量事件总数"] == incident_window["统计"]["事件总数"] == 1
        for item in report["窗口明细"]:
            assert item["归属日期"] == "2026-11-20"

    def test_dispatch_list_written_from_window_details(self):
        _seed_pair()
        svc = DqReportService()
        report = svc.aggregate_quality_report("daily", "2026-11-20")

        dispatches = svc.get_dispatch_list(report["报告编号"])
        assert len(dispatches) == report["质量事件总数"] == 1
        dispatch = dispatches[0]
        assert dispatch["事件编号"] == "DQ_I-0004"
        assert dispatch["归属日期"] == "2026-11-20"
        assert dispatch["窗口快照编号"] == report["窗口快照编号"]
        assert dispatch["是否跨日补入"] is True
        assert dispatch["领取状态"] == "待领取"

    def test_claim_returns_occurrence_day(self):
        _seed_pair()
        svc = DqReportService()
        report = svc.aggregate_quality_report("daily", "2026-11-20")
        claim = svc.claim_dispatch(report["报告编号"], "张三")
        assert claim["领取事件数"] == 1
        assert claim["按归属日期"] == {"2026-11-20": 1}
        # 再次领取不会重复派工。
        assert svc.claim_dispatch(report["报告编号"], "李四")["领取事件数"] == 1

    def test_adjacent_days_claim_late_event_only_once(self):
        """按题目要求走一遍：先查第一个日期，再查相邻日期，核对总数。"""
        _seed_pair()
        svc = DqReportService()
        first = svc.aggregate_quality_report("daily", "2026-11-20")
        next_day = svc.aggregate_quality_report("daily", "2026-11-21")

        # 补入的订单事件在 11-20 被领取，11-21 的派工里没有它。
        first_claim = svc.claim_dispatch(first["报告编号"])
        next_claim = svc.claim_dispatch(next_day["报告编号"])
        assert [item["事件编号"] for item in first_claim["派工清单"]] == ["DQ_I-0004"]
        assert "DQ_I-0004" not in [
            item["事件编号"] for item in next_claim["派工清单"]]
        assert first_claim["按归属日期"] == {"2026-11-20": 1}
        assert next_claim["按归属日期"] == {"2026-11-21": 1}

        # 两天的总数 = 各窗口去重后之和：跨日补入只算一次，其他库同名表单独一条。
        assert first["质量事件总数"] == 1
        assert next_day["质量事件总数"] == 1
        assert first["质量事件总数"] + next_day["质量事件总数"] == 2
        assert next_day["窗口明细"][0]["库名"] == "archive_db"

    def test_report_and_incident_detail_share_snapshot(self):
        _seed_pair()
        svc = DqReportService()
        report = svc.aggregate_quality_report("daily", "2026-11-20")

        # 报告入口：走路由函数，确认返回同一快照编号。
        report_id = int(report["报告编号"].replace("DQ_R-", ""))
        detail = quality_daily.report_detail(report_id)
        assert detail["报告"]["窗口快照编号"] == report["窗口快照编号"]
        assert detail["窗口快照"]["快照编号"] == report["窗口快照编号"]
        assert len(detail["派工清单"]) == 1

        incident = quality_daily.incident_detail("DQ_I-0004")
        assert incident["窗口快照编号"] == report["窗口快照编号"]
        assert incident["归属日期"] == "2026-11-20"
        assert incident["合并记录id"] == [4]

    def test_generate_endpoint_accepts_scope(self):
        _seed_pair()
        payload = quality_daily.generate_daily_report(
            report_date="2026-11-21", days=1,
            db_name="order_db", table_name="ods_order_info",
        )
        assert payload["质量事件总数"] == 0

        payload = quality_daily.generate_daily_report(
            report_date="2026-11-20", days=1,
            db_name="order_db", table_name="ods_order_info",
        )
        assert payload["质量事件总数"] == 1
        assert payload["窗口明细"][0]["库名"] == "order_db"
