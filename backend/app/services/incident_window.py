"""质量事件时间窗口与库表身份解析。

质量事件历史上存在两套时间字段：
1. 新写入的事件同时记录 ``发生时间``（业务实际发生）和 ``接收时间``（平台收到）；
2. 早期事件只记录 ``触发时间``，部分迟到事件只有 ``上报时间``/``入库时间``。

日报必须按“业务发生日”归属，跨日补入的事件回填到发生当天；同一事件被
并发更正后可能留下多条记录，窗口内按事件身份去重后只计一次。

库表身份同样需要兼容：新记录使用 ``所属库名`` + ``目标表名``，历史记录可能
只写了 ``库表标识`` 或从 ``关联规则`` 前缀中能解析出表名。只有库、表都明确
的记录才进入日报统计，身份不完整的旧记录单独列出核对，避免按表名子串串表。
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable

# 业务日界线固定按东八区计算，避免服务器 UTC 时区把“昨晚”算成“今天”。
BUSINESS_TZ = timezone(timedelta(hours=8))

OCCURRENCE_FIELDS = (
    "发生时间",
    "事件发生时间",
    "实际发生时间",
    "触发时间",
    "triggered_at",
    "occurred_at",
    "occurred_time",
    "trigger_time",
)

RECEIVED_FIELDS = (
    "接收时间",
    "上报时间",
    "入库时间",
    "到达时间",
    "更正时间",
    "received_at",
    "received_time",
    "ingested_at",
    "correction_time",
)

EVENT_KEY_FIELDS = (
    "事件编号",
    "事件指纹",
    "event_id",
    "event_no",
    "fingerprint",
    "原事件编号",
)

EXPLICIT_DB_FIELDS = (
    "所属库名",
    "库名",
    "目标库名",
    "数据库",
    "database",
    "db_name",
    "db",
)

EXPLICIT_TABLE_FIELDS = (
    "目标表名",
    "表名",
    "关联表名",
    "目标表",
    "table_name",
    "table",
)

COMBINED_IDENTITY_FIELDS = (
    "库表标识",
    "目标库表",
    "库表",
    "qualified_table",
    "qualified_name",
)

LEGACY_RULE_PREFIXES = {
    "metric_monitor",
    "metric",
    "trend",
    "baseline",
    "anomaly",
    "check_run",
    "rule",
    "table",
    "incident",
}


def _clean_name(value: Any) -> str:
    """库名/表名归一化：去掉空白、反引号和双引号，统一大小写比较。"""
    if value is None:
        return ""
    return str(value).strip().strip("`\"'").strip().casefold()


def _first_value(row: dict[str, Any], fields: Iterable[str]) -> tuple[Any, str | None]:
    for field in fields:
        value = row.get(field)
        if value is not None and str(value).strip() != "":
            return value, field
    return None, None


def parse_event_time(value: Any) -> datetime | None:
    """把历史数据里的各种时间写法解析成东八区 aware datetime。"""
    if value is None:
        return None
    dt: datetime | None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    elif isinstance(value, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        text = text.replace("/", "-")
        dt = None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in (
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y-%m-%d",
                "%Y%m%d%H%M%S",
                "%Y%m%d",
                "%Y-%m-%dT%H:%M:%S",
            ):
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=BUSINESS_TZ)
    return dt.astimezone(BUSINESS_TZ)


def parse_report_date(value: Any) -> date:
    if value is None or str(value).strip() == "":
        return datetime.now(BUSINESS_TZ).date()
    if isinstance(value, datetime):
        return parse_event_time(value).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    parsed = parse_event_time(value)
    if parsed is None:
        raise ValueError(f"无法识别的日期：{value}")
    return parsed.date()


def event_occurrence(row: dict[str, Any]) -> tuple[datetime | None, str]:
    """返回事件“实际发生”时间及来源字段，兼容历史触发时间。"""
    value, field = _first_value(row, OCCURRENCE_FIELDS)
    if field is not None:
        parsed = parse_event_time(value)
        if parsed is not None:
            return parsed, field
    # 只有接收时间的历史记录：用接收时间兜底归属，但保留来源，便于人工核对。
    value, field = _first_value(row, RECEIVED_FIELDS)
    if field is not None:
        parsed = parse_event_time(value)
        if parsed is not None:
            return parsed, f"{field}(兜底)"
    return None, ""


def event_received(row: dict[str, Any], occurrence: datetime | None = None) -> tuple[datetime | None, str]:
    """返回事件接收/上报时间；缺省时退回发生时间，保证明细可核对。"""
    value, field = _first_value(row, RECEIVED_FIELDS)
    if field is not None:
        parsed = parse_event_time(value)
        if parsed is not None:
            return parsed, field
    if occurrence is not None:
        return occurrence, "未记录(按发生时间)"
    return None, ""


def _split_qualified_name(text: str) -> tuple[str, str]:
    text = str(text).strip().strip("`\"'")
    if "." in text:
        db, table = text.rsplit(".", 1)
        return db.strip(), table.strip()
    return "", text


def _identity_from_legacy_rule(rule: str) -> tuple[str, str, str]:
    """从历史 ``关联规则`` 文本中提取库表，不做子串猜测。"""
    text = str(rule or "").strip()
    if not text:
        return "", "", ""
    prefix = ""
    rest = text
    if ":" in text:
        prefix, rest = text.split(":", 1)
        prefix = prefix.strip().casefold()
        rest = rest.strip()
        if prefix not in LEGACY_RULE_PREFIXES:
            return "", "", ""
    if "." not in rest:
        # trend:ods_order_info / baseline:table 这类前缀可以安全地取表名；
        # 裸规则编号（如 DQ_R-0001）不当作表名。
        return ("", rest, "关联规则") if prefix else ("", "", "")
    parts = rest.split(".")
    if prefix in ("metric_monitor", "metric") and len(parts) >= 2:
        # metric_monitor:ods_order_info.amount / metric_monitor:db.table.amount
        if len(parts) >= 3:
            return parts[0], parts[1], "关联规则"
        return "", parts[0], "关联规则"
    if prefix:
        # trend:db.table 之类
        return parts[0], ".".join(parts[1:]), "关联规则"
    # 没有前缀的 db.table
    return parts[0], ".".join(parts[1:]), "关联规则"


def resolve_identity(row: dict[str, Any]) -> dict[str, Any]:
    """解析事件的库表身份，返回原始名、归一化名、来源及是否完整。"""
    db_value, db_field = _first_value(row, EXPLICIT_DB_FIELDS)
    table_value, table_field = _first_value(row, EXPLICIT_TABLE_FIELDS)
    db = str(db_value).strip() if db_value is not None else ""
    table = str(table_value).strip() if table_value is not None else ""
    source = ""
    if db or table:
        source = "字段"

    if (not db or not table):
        combined_value, combined_field = _first_value(row, COMBINED_IDENTITY_FIELDS)
        if combined_value is not None:
            combined_db, combined_table = _split_qualified_name(str(combined_value))
            if not db and combined_db:
                db = combined_db
            if not table and combined_table:
                table = combined_table
            source = source or "库表标识"

    if not db or not table:
        rule_db, rule_table, rule_source = _identity_from_legacy_rule(
            str(row.get("关联规则", ""))
        )
        if not db and rule_db:
            db = rule_db
        if not table and rule_table:
            table = rule_table
        if rule_source:
            source = f"{source}+{rule_source}" if source else rule_source

    return {
        "db": _clean_name(db),
        "table": _clean_name(table),
        "db_raw": db,
        "table_raw": table,
        "source": source or "缺失",
        "complete": bool(db and table),
    }


def event_number(row: dict[str, Any]) -> str:
    value, _ = _first_value(row, EVENT_KEY_FIELDS)
    return str(value).strip() if value is not None else ""


def dedupe_key(row: dict[str, Any], identity: dict[str, Any],
               occurrence: datetime | None) -> str:
    """同一事件的身份键：事件编号优先，其次指纹，最后规则+库表+发生时间。"""
    number = event_number(row)
    if number:
        return f"event:{number.casefold()}"
    fingerprint = str(row.get("事件指纹") or row.get("fingerprint") or "").strip()
    if fingerprint:
        return f"fp:{fingerprint.casefold()}"
    rule = str(row.get("关联规则") or "").strip().casefold()
    return "composite:{db}|{table}|{rule}|{occ}".format(
        db=identity["db"],
        table=identity["table"],
        rule=rule,
        occ=occurrence.isoformat() if occurrence else "",
    )


def _relevance(identity: dict[str, Any], target_db: str, target_table: str) -> str:
    """判断一条记录相对筛选范围的关系：match / manual / out。

    - 指定表名时，表名不相等直接排除；连表名都没有的记录无法归属，进待核对；
    - 指定库名时，库名不等排除；库名缺失但表名对得上的记录进待核对；
    - 不指定范围（整份日报）时，缺少表名的旧记录进待核对，不参与确认统计。
    """
    db, table = identity["db"], identity["table"]
    if target_table:
        if not table:
            return "manual"
        if table != target_table:
            return "out"
        if target_db:
            if not db:
                return "manual"
            return "match" if db == target_db else "out"
        return "match"
    if target_db:
        if not db:
            return "manual"
        if db != target_db:
            return "out"
        return "match" if table else "manual"
    return "match" if table else "manual"


def _manual_reason(identity: dict[str, Any], occurrence: datetime | None) -> str:
    if occurrence is None:
        return "缺少发生时间"
    if not identity["table"] and not identity["db"]:
        return "库表身份缺失"
    if not identity["table"]:
        return "表名缺失"
    if not identity["db"]:
        return "库名缺失"
    return "库表身份待核对"


def _sort_time(value: datetime | None) -> float:
    return value.timestamp() if value is not None else 0.0


def _make_item(row: dict[str, Any], identity: dict[str, Any],
               occurrence: datetime | None, occurrence_source: str,
               received: datetime | None, received_source: str) -> dict[str, Any]:
    row_id = int(row.get("id", 0) or 0)
    item = {
        "id": row_id,
        "源记录id": row_id,
        "事件编号": event_number(row) or f"row-{row_id}",
        "关联规则": row.get("关联规则", ""),
        "触发时间": row.get("触发时间", ""),
        "库名": identity["db_raw"],
        "表名": identity["table_raw"],
        "目标表名": identity["table_raw"],
        "所属库名": identity["db_raw"],
        "身份来源": identity["source"],
        "发生时间": occurrence.isoformat() if occurrence else "",
        "发生时间来源": occurrence_source,
        "接收时间": received.isoformat() if received else "",
        "接收时间来源": received_source,
        "归属日期": occurrence.strftime("%Y-%m-%d") if occurrence else "",
        "接收日期": received.strftime("%Y-%m-%d") if received else "",
        "是否跨日补入": bool(
            received is not None and occurrence is not None
            and received.date() > occurrence.date()
        ),
        "严重级别": row.get("严重级别", "info") or "info",
        "处理状态": row.get("处理状态", row.get("status", "")),
        "status": row.get("status", ""),
        "偏离程度": row.get("偏离程度", ""),
        "影响行数": row.get("影响行数", ""),
        "处理人": row.get("处理人", ""),
        "合并记录id": [],
        "重复记录数": 0,
        "_dedupe_key": dedupe_key(row, identity, occurrence),
        "_occurrence_sort": _sort_time(occurrence),
        "_received_sort": _sort_time(received),
        "_id": row_id,
    }
    return item


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in item.items() if not key.startswith("_")}


def _manual_item(row: dict[str, Any], identity: dict[str, Any],
                 occurrence: datetime | None, reason: str, *,
                 window_start: datetime, window_end: datetime) -> dict[str, Any] | None:
    row_id = int(row.get("id", 0) or 0)
    received, _ = event_received(row, occurrence)
    return {
        "源记录id": row_id,
        "事件编号": event_number(row) or f"row-{row_id}",
        "关联规则": row.get("关联规则", ""),
        "库名": identity["db_raw"],
        "表名": identity["table_raw"],
        "身份来源": identity["source"],
        "归属日期": occurrence.strftime("%Y-%m-%d") if occurrence else "",
        "发生时间": occurrence.isoformat() if occurrence else "",
        "接收时间": received.isoformat() if received else "",
        "原因": reason,
        "窗口起": window_start.isoformat(),
        "窗口止": window_end.isoformat(),
    }


def build_incident_window(
    rows: Iterable[dict[str, Any]],
    *,
    report_date: Any = None,
    days: int = 1,
    db_name: str | None = None,
    table_name: str | None = None,
) -> dict[str, Any]:
    """构建某个业务日（或连续 N 天）的质量事件窗口快照。

    - 按 ``发生时间`` 归属自然日，跨日补入记录回填到发生当天；
    - 表名/库名都做精确比较，杜绝子串串表；
    - 同一事件编号（或指纹/复合键）并发更正只保留一条，并记录被合并的记录；
    - 库表身份不完整的旧记录进入 ``待核对``，不计入确认统计。
    """
    days = max(1, int(days))
    end_day = parse_report_date(report_date)
    start_day = end_day - timedelta(days=days - 1)
    window_start = datetime.combine(start_day, time.min, tzinfo=BUSINESS_TZ)
    window_end = datetime.combine(end_day + timedelta(days=1), time.min, tzinfo=BUSINESS_TZ)

    target_db = _clean_name(db_name)
    target_table = _clean_name(table_name)

    confirmed: list[dict[str, Any]] = []
    manual: list[dict[str, Any]] = []
    raw_confirmed_count = 0

    records = []
    for row in rows:
        occurrence, occurrence_source = event_occurrence(row)
        received, received_source = event_received(row, occurrence)
        identity = resolve_identity(row)
        records.append({
            "row": row,
            "occurrence": occurrence,
            "occurrence_source": occurrence_source,
            "received": received,
            "received_source": received_source,
            "identity": identity,
        })

    # 先按事件编号做全局去重：并发更正即使跨天、把发生时间改到次日，
    # 也只保留接收时间最新的一条参与归窗，避免两天各算一次。
    grouped_numbers: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        number = event_number(record["row"])
        if number:
            grouped_numbers.setdefault(number.casefold(), []).append(record)
    merged_by_winner: dict[int, list[int]] = {}
    group_size_by_winner: dict[int, int] = {}
    dropped: set[int] = set()
    identity_override: dict[int, dict[str, Any]] = {}
    for group in grouped_numbers.values():
        if len(group) < 2:
            continue
        winner = max(group, key=lambda rec: (
            _sort_time(rec["received"]), int(rec["row"].get("id", 0) or 0)))
        winner_id = int(winner["row"].get("id", 0) or 0)
        loser_ids = sorted(
            int(rec["row"].get("id", 0) or 0) for rec in group if rec is not winner
        )
        merged_by_winner[winner_id] = loser_ids
        group_size_by_winner[winner_id] = len(group)
        for rec in group:
            if rec is not winner:
                dropped.add(id(rec))
        # 更正记录可能没带全库表身份，从同组记录里补一个完整的身份。
        if not winner["identity"]["complete"]:
            complete = next(
                (rec["identity"] for rec in group if rec["identity"]["complete"]), None)
            if complete is not None:
                identity_override[winner_id] = complete

    for record in records:
        if id(record) in dropped:
            continue
        row = record["row"]
        occurrence = record["occurrence"]
        received = record["received"]
        identity = identity_override.get(
            int(row.get("id", 0) or 0), record["identity"])
        relevance = _relevance(identity, target_db, target_table)

        if occurrence is None:
            # 没有发生时间的旧记录无法确定归属日：只在“今天”的日报里列出待核对，
            # 避免同一批无时间记录在每一份历史日报里重复出现、反复核对。
            today = datetime.now(BUSINESS_TZ).date()
            if relevance != "out" and end_day == today:
                item = _manual_item(
                    row, identity, None, _manual_reason(identity, None),
                    window_start=window_start, window_end=window_end,
                )
                if item is not None:
                    manual.append(item)
            continue

        # 时间窗口严格按业务发生日过滤：昨天的事件不会因为今天才收到而进入今天日报。
        if not (window_start <= occurrence < window_end):
            continue
        if relevance == "out":
            continue

        if relevance == "manual" or not identity["complete"]:
            manual_item = _manual_item(
                row, identity, occurrence, _manual_reason(identity, occurrence),
                window_start=window_start, window_end=window_end,
            )
            if manual_item is not None:
                manual.append(manual_item)
            continue

        raw_confirmed_count += group_size_by_winner.get(
            int(row.get("id", 0) or 0), 1)
        item = _make_item(
            row, identity, occurrence, record["occurrence_source"],
            received, record["received_source"],
        )
        item["合并记录id"] = merged_by_winner.get(item["源记录id"], [])
        item["重复记录数"] = len(item["合并记录id"])
        confirmed.append(item)

    # 没有事件编号的记录只能在窗口内按复合键去重（复合键已包含发生时间）。
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in confirmed:
        grouped.setdefault(item["_dedupe_key"], []).append(item)

    details: list[dict[str, Any]] = []
    for group in grouped.values():
        winner = max(group, key=lambda it: (it["_received_sort"], it["_id"]))
        merged_ids = sorted(
            it["_id"] for it in group if it["_id"] != winner["_id"]
        )
        if merged_ids:
            winner["合并记录id"] = sorted(set(winner["合并记录id"]) | set(merged_ids))
            winner["重复记录数"] = len(winner["合并记录id"])
        details.append(winner)

    # 合并数同时覆盖跨日全局去重和窗口内复合键去重。
    merged_count = sum(item["重复记录数"] for item in details)

    # 待核对清单同样按事件身份去重，并发更正不会在核对表里重复出现。
    manual_grouped: dict[str, dict[str, Any]] = {}
    for item in manual:
        key = item["事件编号"]
        key = key if not key.startswith("row-") else f"row:{item['源记录id']}"
        if key not in manual_grouped:
            manual_grouped[key] = item
    manual = list(manual_grouped.values())

    details.sort(key=lambda it: (it["_occurrence_sort"], it["_id"]))
    manual.sort(key=lambda it: (it.get("发生时间", ""), it.get("源记录id", 0)))

    by_level = {"critical": 0, "warning": 0, "info": 0}
    by_status: dict[str, int] = {}
    by_table: dict[str, int] = {}
    by_day: dict[str, int] = {}
    root_causes: dict[str, int] = {}
    for item in details:
        level = str(item.get("严重级别") or "info")
        by_level[level] = by_level.get(level, 0) + 1
        status = str(item.get("status") or item.get("处理状态") or "待确认")
        by_status[status] = by_status.get(status, 0) + 1
        table_key = item["表名"] or "未标表"
        db_key = item["库名"] or "未标库"
        qualified = f"{db_key}.{table_key}"
        by_table[qualified] = by_table.get(qualified, 0) + 1
        by_day[item["归属日期"]] = by_day.get(item["归属日期"], 0) + 1
        cause = str(item.get("关联规则") or "未知")
        root_causes[cause] = root_causes.get(cause, 0) + 1

    stats = {
        "事件总数": len(details),
        "原始记录数": raw_confirmed_count,
        "去重后": len(details),
        "合并重复记录数": merged_count,
        "按级别": by_level,
        "按状态": by_status,
        "按库表": by_table,
        "归属日期分布": by_day,
        "根因分布": dict(
            sorted(root_causes.items(), key=lambda pair: pair[1], reverse=True)[:5]
        ),
    }

    return {
        "报告日期": end_day.isoformat(),
        "归属日期": end_day.isoformat(),
        "起始日期": start_day.isoformat(),
        "结束日期": end_day.isoformat(),
        "天数": days,
        "窗口起": window_start.isoformat(),
        "窗口止": window_end.isoformat(),
        "库表范围": {
            "库名": db_name or "",
            "表名": table_name or "",
        },
        "筛选口径": (
            "按发生时间归属业务自然日；跨日补入回填发生日；"
            "库表精确匹配；同一事件按事件编号去重"
        ),
        "明细": [_public_item(item) for item in details],
        "统计": stats,
        "待核对": manual,
        "待核对数量": len(manual),
        "生成时间": datetime.now(BUSINESS_TZ).isoformat(),
    }


def matches_scope(row: dict[str, Any], *, db_name: str | None = None,
                  table_name: str | None = None) -> bool:
    """不带时间窗口时的精确库表匹配，供趋势分析和事件列表复用。

    未指定库/表时不过滤；指定表名时必须有解析出的表身份并完全相等，
    指定库名时必须库名相等——缺库名的旧记录不会被误算进某个库。
    """
    identity = resolve_identity(row)
    target_db = _clean_name(db_name)
    target_table = _clean_name(table_name)
    if target_table and identity["table"] != target_table:
        return False
    if target_db and identity["db"] != target_db:
        return False
    return True
