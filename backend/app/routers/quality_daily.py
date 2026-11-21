"""质量日报接口：报告入口、窗口明细、派工清单和事件详情共用同一份快照。

路径都放在 /api/dq_report/... 之外，避免被通用资源路由 /api/{module}/{entry_id}
误认为是模块 id。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.services.dq_report import DqReportService
from app.store import store

router = APIRouter(prefix="/api/quality-daily", tags=["质量日报"])


def _service() -> DqReportService:
    return DqReportService()


@router.post("/generate")
def generate_daily_report(
    report_date: str | None = Query(default=None, description="业务日，默认今天"),
    days: int = Query(default=1, ge=1, le=31),
    db_name: str | None = Query(default=None),
    table_name: str | None = Query(default=None),
) -> dict[str, Any]:
    """生成质量日报：先落窗口快照，再回写派工清单。"""
    try:
        return _service().aggregate_quality_report(
            "daily", report_date, days=days,
            db_name=db_name, table_name=table_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/reports")
def list_daily_reports() -> dict[str, Any]:
    rows = [
        row for row in store.rows("dq_report")
        if row.get("窗口快照编号")
    ]
    return {"items": rows, "total": len(rows)}


@router.get("/reports/{report_id}")
def report_detail(report_id: int) -> dict[str, Any]:
    """报告入口：返回报告本身 + 关联的窗口快照编号和派工清单。"""
    report = store.find("dq_report", report_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"质量报告 {report_id} 不存在")
    service = _service()
    # 报告里保存的是快照业务编号（DQ_W-xxxx），不要按数字主键去猜。
    snapshot = service.find_window_snapshot(
        report.get("窗口快照编号") or report.get("报告编号") or report_id
    )
    return {
        "报告": report,
        "窗口快照": snapshot,
        "派工清单": service.get_dispatch_list(report.get("报告编号") or report_id),
    }


@router.get("/snapshots/{reference}")
def snapshot_detail(reference: str) -> dict[str, Any]:
    """按快照编号（DQ_W-xxxx）或报告编号（DQ_R-xxxx）读取窗口明细。"""
    snapshot = _service().find_window_snapshot(reference)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"窗口快照 {reference} 不存在")
    return snapshot


@router.get("/dispatches")
def dispatch_list(reference: str = Query(description="报告编号或快照编号")) -> dict[str, Any]:
    items = _service().get_dispatch_list(reference)
    return {"items": items, "total": len(items)}


@router.post("/dispatches/claim")
def claim_dispatches(
    reference: str = Query(description="报告编号或快照编号"),
    claimer: str = Query(default="数据工程组"),
) -> dict[str, Any]:
    return _service().claim_dispatch(reference, claimer)


@router.get("/incidents/{incident_ref}")
def incident_detail(incident_ref: str) -> dict[str, Any]:
    """事件详情：关联到它所在的窗口明细，给出归属日期和被合并的重复记录。"""
    service = _service()
    snapshot, item = _locate_incident(service, incident_ref)
    if item is None:
        raise HTTPException(status_code=404, detail=f"窗口内没有事件 {incident_ref}")
    return {
        "事件": item,
        "窗口快照编号": snapshot.get("快照编号") if snapshot else "",
        "归属日期": item.get("归属日期"),
        "报告日期": snapshot.get("报告日期") if snapshot else "",
        "合并记录id": item.get("合并记录id", []),
        "同窗口其他事件": [
            other for other in (snapshot.get("明细") or [])
            if other.get("事件编号") != item.get("事件编号")
        ] if snapshot else [],
    }


def _locate_incident(service: DqReportService, incident_ref: str):
    """在最近的窗口快照里定位事件，返回（快照, 明细项）。"""
    target = str(incident_ref).strip()
    snapshots = list(store.rows("dq_report_snapshot"))
    for snapshot in reversed(snapshots):
        for item in snapshot.get("明细") or []:
            if target in {
                str(item.get("事件编号")),
                str(item.get("源记录id")),
                f"row-{item.get('源记录id')}",
            }:
                return snapshot, item
    return None, None
