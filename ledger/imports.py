"""Explicit, request-level import contracts. Aggregate reports have no importer."""

import csv
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
    mark_usage_changes,
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


def import_file(db, raw, fmt):
    content = raw.decode("utf-8-sig")
    if fmt == "csv":
        rows = csv.DictReader(io.StringIO(content, newline=""))
    else:
        doc = json.loads(content)
        rows = doc if isinstance(doc, list) else [doc]
    added = duplicate = 0
    entries = set()
    with db:
        db.execute("BEGIN IMMEDIATE")
        before = db.total_changes
        for n, item in enumerate(rows, 1):
            try:
                rec = convert(item, fmt == "csv")
                old = db.execute("SELECT * FROM events WHERE uid=?", (rec[0],)).fetchone()
                if old:
                    # Source label and project annotation may differ between two exports of one request.
                    if (old["entry"], old["ts"], old["model"]) != (rec[2], rec[1], rec[4]) or any(
                        old[key] is not None and v is not None and old[key] != v
                        for key, v in zip(FIELDS, rec[7:])
                    ):
                        raise ValueError("同一请求标识出现相冲突的时间、入口、模型或计数")
                    merged = counters(
                        **{k: old[k] if old[k] is not None else v for k, v in zip(FIELDS, rec[7:])}
                    )
                    if any(old[k] != merged[k] for k in FIELDS):
                        db.execute(
                            "UPDATE events SET total=?,input=?,output=?,cache_read=?,cache_write=?,reasoning=? WHERE uid=?",
                            (*[merged[k] for k in FIELDS], rec[0]),
                        )
                    duplicate += 1
                else:
                    db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rec)
                    added += 1
                entries.add(rec[2])
            except (KeyError, TypeError, AttributeError, ValueError, OverflowError, OSError) as err:
                raise ValueError(
                    f"第 {n} 条记录格式无效。{err}"
                    if type(err) is ValueError
                    else f"第 {n} 条记录字段类型无效"
                ) from None
        if not entries:
            raise ValueError("文件没有可导入的逐请求记录")
        mark_usage_changes(db, before)
        for entry in entries:
            db.execute(
                "INSERT OR REPLACE INTO scans VALUES (?,?,NULL,0,0,1)",
                (entry, datetime.now(timezone.utc).isoformat()),
            )
    return {"added": added, "duplicate": duplicate}
