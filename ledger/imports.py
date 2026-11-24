"""Explicit, request-level import contracts. Aggregate reports have no importer."""

import hashlib
import csv
from .evidence import merge, now, pack, write_event
import io
import json
from datetime import datetime, timezone

from .core import (
    ENTRIES,
    FIELDS,
    count,
    counters,
    digest,
    event,
    stamp,
    text_field,
)


def native_usage(entry, response):
    u = response.get("usage") or {}
    if entry == "openai-api":
        if "input_tokens" in u:
            return counters(
                u.get("total_tokens"),
                u.get("input_tokens"),
                u.get("output_tokens"),
                (u.get("input_tokens_details") or {}).get("cached_tokens"),
                (u.get("input_tokens_details") or {}).get("cache_write_tokens"),
                reasoning=(u.get("output_tokens_details") or {}).get("reasoning_tokens"),
            )
        return counters(
            u.get("total_tokens"),
            u.get("prompt_tokens"),
            u.get("completion_tokens"),
            (u.get("prompt_tokens_details") or {}).get("cached_tokens"),
            (u.get("prompt_tokens_details") or {}).get("cache_write_tokens"),
            reasoning=(u.get("completion_tokens_details") or {}).get("reasoning_tokens"),
        )
    if entry in ("gemini-api", "ai-studio"):
        u = response.get("usageMetadata") or {}
        prompt, candidate = count(u.get("promptTokenCount")), count(u.get("candidatesTokenCount"))
        thought, tool = count(u.get("thoughtsTokenCount")), count(u.get("toolUsePromptTokenCount"))
        total = count(u.get("totalTokenCount"))
        inp = prompt + (tool or 0) if prompt is not None else None
        out = candidate + (thought or 0) if candidate is not None else None
        # Missing optional addends are only treated as zero when the reported total confirms it.
        if (tool is None or thought is None) and (inp is None or out is None or total != inp + out):
            inp = inp if tool is not None else None
            out = out if thought is not None else None
        return counters(total, inp, out, u.get("cachedContentTokenCount"), reasoning=thought)
    if entry == "claude-api":
        fresh = count(u.get("input_tokens"))
        read, write = (
            count(u.get("cache_read_input_tokens")),
            count(u.get("cache_creation_input_tokens")),
        )
        inp = fresh + read + write if None not in (fresh, read, write) else None
        return counters(
            input=inp, output=u.get("output_tokens"), cache_read=read, cache_write=write
        )
    raise ValueError("此入口请使用标准逐请求明细")


def convert(row, csv_input=False):
    if not isinstance(row, dict):
        raise ValueError("每条记录须为对象")
    schema, entry = row.get("schema"), row.get("entry")
    if entry not in ENTRIES or ENTRIES[entry][2] != "import":
        raise ValueError("入口不支持逐请求文件导入。AGY 与 Codex 从本机原始记录采集")
    if row.get("fictional") or row.get("estimated"):
        raise ValueError("示例和估算数据不能计入实测账本")
    vendor = ENTRIES[entry][1]
    if schema == "meter.usage.v1":
        allowed = {"schema", "entry", "request_id", "timestamp", "project", "model", *FIELDS}
        if set(row) - allowed:
            raise ValueError("标准格式含未定义字段，请使用接入说明中的列")

        def number(key):
            value = row.get(key)
            if csv_input:
                return None if value in ("", None) else int(value)
            return value

        value = counters(**{k: number(k) for k in FIELDS})
        rid, when, model = row.get("request_id"), row.get("timestamp"), row.get("model")
        source = "逐请求明细 · v1"
    elif schema == "meter.response.v1":
        response = row.get("response")
        if not isinstance(response, dict):
            raise ValueError("缺少原始响应的统计对象 response")
        value = native_usage(entry, response)
        rid = response.get("id") or response.get("responseId") or row.get("request_id")
        model = response.get("modelVersion") or response.get("model")
        when = row.get("timestamp") or response.get("createTime")
        created = response.get("created_at", response.get("created"))
        if not when and type(created) in (float, int):
            when = datetime.fromtimestamp(created, timezone.utc).isoformat()
        source = "厂商响应 · usage"
    else:
        raise ValueError("无法识别的版本。请导入带 schema 的逐请求明细")
    rid = text_field(rid, 256)
    if not rid:
        raise ValueError("缺少原始请求的稳定标识 request_id")
    model = text_field(model)
    if entry in ("gemini-api", "ai-studio"):
        model = model.removeprefix("models/")
    uid = digest("api-request", vendor, rid)
    return event(
        uid, stamp(when), entry, vendor, model, text_field(row.get("project")), source, value
    )


def import_file(db, raw, fmt, preview=False):
    content = raw.decode("utf-8-sig")
    if fmt == "csv":
        rows = csv.DictReader(io.StringIO(content, newline=""), strict=True)
    elif fmt == "json":
        doc = json.loads(content)
        rows = doc if isinstance(doc, list) else [doc]
    else:
        raise ValueError("请选择 JSON 或 CSV")
    result = dict(added=0, duplicate=0, enriched=0, conflict=0, invalid=0, problems=[])
    entries = set()
    with db:
        db.execute("BEGIN IMMEDIATE")
        batch = db.execute(
            "INSERT INTO batches(created,fingerprint) VALUES(?,?)",
            (now(), hashlib.sha256(raw).hexdigest()),
        ).lastrowid
        try:
            for n, item in enumerate(rows, 1):
                try:
                    rec = convert(item, fmt == "csv")
                except (
                    KeyError,
                    TypeError,
                    AttributeError,
                    ValueError,
                    OverflowError,
                    OSError,
                ) as err:
                    result["invalid"] += 1
                    if len(result["problems"]) < 50:
                        result["problems"].append(
                            f"第 {n} 行：{err if isinstance(err, ValueError) else '字段格式无效'}"
                        )
                    continue
                old = db.execute("SELECT * FROM events WHERE uid=?", (rec[0],)).fetchone()
                try:
                    merged = merge(tuple(old), rec) if old else rec
                except ValueError as err:
                    result["conflict"] += 1
                    if len(result["problems"]) < 50:
                        result["problems"].append(f"第 {n} 行：{err}")
                    continue
                if (
                    old
                    and not db.execute(
                        "SELECT 1 FROM contributions WHERE uid=? LIMIT 1", (rec[0],)
                    ).fetchone()
                ):
                    db.execute(
                        "INSERT OR IGNORE INTO legacy_base VALUES(?,?)", (rec[0], pack(tuple(old)))
                    )
                kind = "added" if not old else "duplicate" if tuple(old) == merged else "enriched"
                result[kind] += 1
                if kind != "duplicate":
                    write_event(db, merged)
                db.execute(
                    "INSERT INTO contributions VALUES(?,?,?,?,?)",
                    (batch, n, rec[0], pack(rec), kind),
                )
                entries.add(rec[2])
        except csv.Error as exc:
            raise ValueError("CSV 结构无效") from exc
        if not sum(result[k] for k in ("added", "enriched", "duplicate", "invalid", "conflict")):
            raise ValueError("文件没有可导入的逐请求记录")
        if preview:
            db.rollback()
            return result
        if result["invalid"] or result["conflict"]:
            raise ValueError(result["problems"][0] + "；整批未入账")
        if result["added"] or result["enriched"]:
            db.execute("UPDATE usage_revision SET value=value+1")
        db.execute("UPDATE batches SET result=? WHERE id=?", (pack(result), batch))
        for entry in entries:
            db.execute("INSERT OR REPLACE INTO scans VALUES(?,?,NULL,0,0,1)", (entry, now()))
    return dict(result, batch=batch)


def undo(db, batch):
    with db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute(
            "UPDATE batches SET undone=? WHERE id=? AND undone IS NULL", (now(), batch)
        ).rowcount:
            raise ValueError("此批次不存在或已撤销")
        affected = [
            r[0]
            for r in db.execute("SELECT DISTINCT uid FROM contributions WHERE batch=?", (batch,))
        ]
        removed = changed = 0
        for uid in affected:
            old = tuple(db.execute("SELECT * FROM events WHERE uid=?", (uid,)).fetchone())
            base = db.execute("SELECT payload FROM legacy_base WHERE uid=?", (uid,)).fetchone()
            rec = tuple(json.loads(base[0])) if base else None
            for row in db.execute(
                """SELECT c.payload FROM contributions c JOIN batches b ON b.id=c.batch
                    WHERE c.uid=? AND b.undone IS NULL ORDER BY c.batch,c.line""",
                (uid,),
            ):
                data = tuple(json.loads(row[0]))
                rec = merge(rec, data) if rec else data
            if rec is None:
                db.execute("DELETE FROM events WHERE uid=?", (uid,))
                removed += 1
            elif rec != old:
                write_event(db, rec)
                changed += 1
        if removed or changed:
            db.execute("UPDATE usage_revision SET value=value+1")
    return dict(removed=removed, recomputed=changed, retained=len(affected) - removed)
