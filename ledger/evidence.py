"""Reconcile observations of one stable request. Persist only accounting metadata."""

import json
from datetime import datetime, timezone

from .core import (
    FIELDS,
    UNKNOWN,
    UNKNOWN_VENDOR,
    ProjectRef,
    counters,
    project_label,
    project_name,
    text_field,
)

COLUMNS = ("uid", "ts", "entry", "vendor", "model", "project", "source", *FIELDS)
MIRROR = "agy-shared"


def now():
    return datetime.now(timezone.utc).isoformat()


def pack(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def unknown(key, value):
    if key == "project":
        return value == UNKNOWN
    if key == "vendor":
        return value == UNKNOWN_VENDOR
    return key == "model" and (
        value in ("型号未记录", "AGY · 型号未记录")
        or (value.startswith("AGY #") and value.endswith("型号未确认"))
    )


def merge(old, new, agy=False):
    result = list(old)
    for i, (key, a, b) in enumerate(zip(COLUMNS, old, new, strict=True)):
        if a == b or key == "source" or b is None or unknown(key, b):
            continue
        if key == "entry" and agy:
            result[i] = MIRROR
        elif a is None or unknown(key, a):
            result[i] = b
        else:
            raise ValueError(f"同一请求的 {key} 互相冲突：{a} ↔ {b}")
    usage = counters(**dict(zip(FIELDS, result[7:], strict=True)))
    result[7:] = [usage[k] for k in FIELDS]
    return tuple(result)


def write_event(db, rec):
    db.execute("INSERT OR REPLACE INTO events VALUES (" + ",".join("?" for _ in rec) + ")", rec)


def observe(db, rec, origin, stats):
    uid = rec[0]
    old = db.execute("SELECT * FROM events WHERE uid=?", (uid,)).fetchone()
    previous = tuple(old) if old else None
    if (
        previous
        and not db.execute("SELECT 1 FROM observations WHERE uid=? LIMIT 1", (uid,)).fetchone()
    ):
        db.execute(
            "INSERT INTO observations(uid,origin,entry,source,payload,seen) VALUES(?,?,?,?,?,?)",
            (uid, "legacy", previous[2], "旧版来源未记录", pack(previous), now()),
        )
    cur = db.execute(
        "INSERT OR IGNORE INTO observations(uid,origin,entry,source,payload,seen) VALUES(?,?,?,?,?,?)",
        (uid, origin, rec[2], rec[6], pack(rec), now()),
    )
    observation = (
        cur.lastrowid
        if cur.rowcount
        else db.execute(
            "SELECT id FROM observations WHERE uid=? AND origin=? AND payload=?",
            (uid, origin, pack(rec)),
        ).fetchone()[0]
    )
    head = db.execute(
        "SELECT observation FROM observation_heads WHERE uid=? AND origin=? AND source=?",
        (uid, origin, rec[6]),
    ).fetchone()
    if head and head[0] == observation:
        return 0
    if head:
        log_change(db, uid, "观测修订", head[0], observation, "同一来源统计行重新读取")
    db.execute(
        "INSERT OR REPLACE INTO observation_heads VALUES(?,?,?,?)",
        (uid, origin, rec[6], observation),
    )
    try:
        result = merge(previous, rec, True) if previous else rec
    except ValueError as exc:
        reason = ("同来源修订待核对" if head else "不同观测不一致") + " · " + str(exc)
        db.execute("INSERT OR REPLACE INTO disputes VALUES(?,?,NULL)", (uid, reason))
        stats["conflicts"] += 1
        result = previous
    if previous and previous[2] != rec[2]:
        result = (*result[:2], MIRROR, *result[3:])
    if previous != result:
        write_event(db, result)
        if previous:
            stats["enriched"] += 1
    return int(old is None)


def log_change(db, target, action, old, new, reason):
    reason = text_field(reason, 300)
    if not reason:
        raise ValueError("请填写更正依据")
    db.execute(
        "INSERT INTO changes(at,target,action,previous,value,reason) VALUES(?,?,?,?,?,?)",
        (now(), target, action, pack(old), pack(new), reason),
    )


def correct(db, data):
    action, uid = data.get("action"), text_field(data.get("target"))
    with db:
        db.execute("BEGIN IMMEDIATE")
        if action == "alias":
            path = text_field(data.get("path"), 4096)
            if path:
                ref = project_name(path)
                if not isinstance(ref, ProjectRef):
                    raise ValueError("请填写工作区的完整路径")
                uid = project_label(db, ref)
            elif (
                uid == UNKNOWN
                or not db.execute(
                    "SELECT 1 FROM events WHERE project=? UNION ALL SELECT 1 FROM aliases WHERE raw=? LIMIT 1",
                    (uid, uid),
                ).fetchone()
            ):
                raise ValueError("找不到原始项目")
            old = db.execute("SELECT logical FROM aliases WHERE raw=?", (uid,)).fetchone()
            new = text_field(data.get("value"))
            if new:
                db.execute("INSERT OR REPLACE INTO aliases VALUES(?,?)", (uid, new))
            else:
                db.execute("DELETE FROM aliases WHERE raw=?", (uid,))
        else:
            if not db.execute("SELECT 1 FROM events WHERE uid=?", (uid,)).fetchone():
                raise ValueError("找不到记录")
            if action == "resolve":
                row = db.execute(
                    "SELECT payload FROM observations WHERE id=? AND uid=?",
                    (data.get("value"), uid),
                ).fetchone()
                dispute = db.execute("SELECT resolved FROM disputes WHERE uid=?", (uid,)).fetchone()
                if not row or not dispute:
                    raise ValueError("找不到所选冲突证据")
                rec = json.loads(row[0])
                old = db.execute("SELECT * FROM events WHERE uid=?", (uid,)).fetchone()
                if old["entry"] == MIRROR:
                    rec[2] = MIRROR
                counters(**dict(zip(FIELDS, rec[7:], strict=True)))
                write_event(db, rec)
                new = int(data["value"])
                db.execute("UPDATE disputes SET resolved=? WHERE uid=?", (new, uid))
            elif action in ("project", "account", "task"):
                old = db.execute(f"SELECT {action} FROM annotations WHERE uid=?", (uid,)).fetchone()
                new = text_field(data.get("value")) or None
                db.execute(
                    f"INSERT INTO annotations(uid,{action}) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET {action}=excluded.{action}",
                    (uid, new),
                )
            else:
                raise ValueError("未知更正操作")
        log_change(db, uid, action, list(old) if old else None, new, data.get("reason"))
        db.execute("UPDATE usage_revision SET value=value+1")
    return {"ok": True}


def review(db, uid):
    current = db.execute("SELECT * FROM ledger_events WHERE uid=?", (uid,)).fetchone()
    observations = []
    for r in db.execute(
        "SELECT o.*, EXISTS(SELECT 1 FROM observation_heads h WHERE h.uid=o.uid AND h.origin=o.origin AND h.source=o.source AND h.observation=o.id) latest FROM observations o WHERE o.uid=? ORDER BY id DESC",
        (uid,),
    ):
        observations.append(
            dict(
                id=r["id"],
                latest=bool(r["latest"]),
                origin=r["origin"],
                seen=r["seen"],
                entry=r["entry"],
                source=r["source"],
                record=dict(zip(COLUMNS, json.loads(r["payload"]))),
            )
        )
    imports = [
        dict(r)
        for r in db.execute(
            """SELECT b.id,b.created,b.undone,c.line,c.payload FROM contributions c
               JOIN batches b ON b.id=c.batch WHERE uid=? ORDER BY b.id,c.line""",
            (uid,),
        )
    ]
    for row in imports:
        row["record"] = dict(zip(COLUMNS, json.loads(row.pop("payload"))))
    legacy = bool(db.execute("SELECT 1 FROM legacy_base WHERE uid=?", (uid,)).fetchone()) or bool(
        current
        and current["entry"]
        in ("openai-api", "gemini-api", "ai-studio", "claude-api", "grok-api", "kimi-api")
        and not imports
    )
    return dict(
        current=dict(current) if current else None,
        disputed=bool(db.execute("SELECT 1 FROM disputes WHERE uid=?", (uid,)).fetchone()),
        observations=observations,
        batches=imports,
        legacy=legacy,
        changes=[
            dict(r)
            for r in db.execute("SELECT * FROM changes WHERE target=? ORDER BY id DESC", (uid,))
        ],
    )


def management(db, page=0):
    limit = 40
    offset = max(page, 0) * limit
    return dict(
        page=page,
        limit=limit,
        batch_count=db.execute("SELECT count(*) FROM batches").fetchone()[0],
        batches=[
            dict(r)
            for r in db.execute(
                "SELECT * FROM batches ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
            )
        ],
        disputes=[
            dict(r)
            for r in db.execute(
                "SELECT d.*,e.model,e.ts FROM disputes d JOIN events e USING(uid) ORDER BY resolved IS NOT NULL,ts DESC"
            )
        ],
        aliases=dict(db.execute("SELECT raw,logical FROM aliases")),
        projects=[
            r[0]
            for r in db.execute(
                "SELECT project FROM events WHERE project<>'未归类' UNION SELECT raw FROM aliases ORDER BY 1"
            )
        ],
        changes=[dict(r) for r in db.execute("SELECT * FROM changes ORDER BY id DESC LIMIT 40")],
    )


def alerts(db):
    rows = []
    for r in db.execute("""SELECT s.*,h.last_read,h.ever_present FROM scans s LEFT JOIN health h USING(entry)
            WHERE s.files IS NOT NULL AND (s.failures>0 OR s.skipped>0 OR s.present=0 OR s.files=0)"""):
        if (
            r["failures"]
            or r["skipped"]
            or r["last_read"]
            or r["ever_present"]
            or db.execute("SELECT 1 FROM events WHERE entry=? LIMIT 1", (r["entry"],)).fetchone()
        ):
            rows.append(r["entry"])
    missing = db.execute("SELECT 1 FROM codex_thread_index WHERE located=0 LIMIT 1").fetchone()
    return dict(
        entries=sorted(set(rows) | ({"codex"} if missing else set())),
        conflicts=db.execute("SELECT count(*) FROM disputes WHERE resolved IS NULL").fetchone()[0],
    )


def batch_rows(db, batch, page):
    if page < 0:
        raise ValueError("页码无效")
    batch_info = db.execute("SELECT * FROM batches WHERE id=?", (batch,)).fetchone()
    if not batch_info:
        raise ValueError("找不到导入批次")
    rows = []
    for r in db.execute(
        """SELECT c.line,c.uid,c.outcome,c.payload,e.total,e.uid present FROM contributions c
            LEFT JOIN events e USING(uid) WHERE c.batch=? ORDER BY c.line LIMIT 41 OFFSET ?""",
        (batch, page * 40),
    ):
        rows.append(
            dict(
                line=r["line"],
                uid=r["uid"],
                outcome=r["outcome"],
                present=bool(r["present"]),
                effective_total=r["total"],
                record=dict(zip(COLUMNS, json.loads(r["payload"]))),
            )
        )
    return dict(batch=dict(batch_info), page=page, rows=rows[:40], more=len(rows) > 40)


def identify_agy(db, rows, anchors, slots=None):
    """Keep a preexisting local UID when a stable request ID subsequently arrives."""
    result, missed = [], 0
    for row, identity in zip(rows, anchors, strict=True):
        anchor, request = identity[:2]
        if request:
            prior = db.execute(
                "SELECT uid FROM agy_keys WHERE request=? LIMIT 1", (request,)
            ).fetchone()
            fallback = db.execute("SELECT 1 FROM events WHERE uid=?", (anchor,)).fetchone()
            uid = prior[0] if prior else request
            if not prior and fallback:
                existing = db.execute("SELECT 1 FROM events WHERE uid=?", (request,)).fetchone()
                other = db.execute(
                    "SELECT 1 FROM agy_keys WHERE anchor=? AND request<>?", (anchor, request)
                ).fetchone()
                if not existing and not other:
                    uid = anchor
                elif existing and not other:
                    db.execute(
                        "INSERT OR IGNORE INTO disputes VALUES(?,?,NULL)",
                        (request, "旧账本同时存在步骤身份及请求身份，需核对旧记录"),
                    )
            db.execute("INSERT OR IGNORE INTO agy_keys VALUES(?,?,?)", (anchor, request, uid))
        else:
            known = db.execute(
                "SELECT DISTINCT uid FROM agy_keys WHERE anchor=? LIMIT 2", (anchor,)
            ).fetchall()
            if len(known) > 1:
                missed += 1
                continue
            uid = known[0][0] if known else anchor
        if slots is not None:
            slots.append(identity[2])
        result.append((uid, *row[1:]))
    return result, missed
