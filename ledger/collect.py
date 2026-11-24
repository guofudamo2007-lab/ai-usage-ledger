"""Read-only local adapters. Nothing in this module opens a source for writing."""

import io
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

from compression.zstd import ZstdError

from .core import (
    UNKNOWN,
    UNKNOWN_VENDOR,
    ProjectRef,
    connect,
    counters,
    digest,
    event,
    ingest,
    model_vendor,
    project_name,
    stamp,
)
from .wire import fields, items, varint
from . import codex_search


def locations():
    gemini = Path.home() / ".gemini"
    codex = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    return {
        "agy": [gemini / "antigravity" / "conversations"],
        "agy-cli": [gemini / "antigravity-cli" / "conversations"],
        "agy-ide": [gemini / "antigravity-ide" / "conversations"],
        "codex": [codex / "sessions", codex / "archived_sessions"],
    }


def ag_project(db):
    names = set()
    try:
        for (blob,) in db.execute("SELECT data FROM trajectory_metadata_blob"):
            for _, workspace in items(blob, {1}):
                folder = fields(workspace, {1}).get(1)
                if isinstance(folder, memoryview) and len(folder) <= 4096:
                    uri = bytes(folder).decode("utf-8")
                    if uri.startswith("file:///"):
                        names.add(project_name(unquote(urlsplit(uri).path.rstrip("/"))))
    except (sqlite3.Error, ValueError, TypeError):
        return UNKNOWN
    return next(iter(names)) if len(names) == 1 else UNKNOWN


def ag_usage(blob):
    raw = fields(blob, {1, 2, 3, 4, 5, 9, 10, 11})
    if not any(k in raw for k in (2, 3, 4, 5)):
        return None
    # ModelUsageStats uses separate proto3 counters for fresh, written and read input.
    read, write = raw.get(5, 0), raw.get(4, 0)
    value = counters(
        input=raw.get(2, 0) + read + write,
        output=raw.get(3, 0),
        cache_read=read,
        cache_write=write,
        reasoning=raw.get(9),
    )
    rid = bytes(raw[11]).hex() if isinstance(raw.get(11), memoryview) else None
    return rid, raw.get(1), value


def ag_timestamp(blob):
    t = fields(blob, {1, 2})
    sec, nanos = t.get(1, 0), t.get(2, 0)
    if not (0 < sec < 7_300_000_000 and 0 <= nanos < 1_000_000_000):
        return None
    return sec * 1_000_000 + nanos // 1000


def ag_model(blob):
    if isinstance(blob, memoryview) and len(blob) <= 150:
        value = bytes(blob).decode("utf-8", "replace")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", value):
            return value
    return None


def read_agy(path, entry, anchors=None, issues=None):
    issues = issues if issues is not None else []

    def issue(table, idx, reason):
        if len(issues) < 30:
            issues.append(f"{table} #{idx} · {reason}")

    wal = Path(str(path) + "-wal")
    live = wal.exists() and wal.stat().st_size > 0
    if live and not Path(str(path) + "-shm").exists():
        raise ValueError("活动 WAL 暂不可只读访问")
    uri = path.resolve().as_uri() + ("?mode=ro" if live else "?mode=ro&immutable=1")
    skipped = 0
    with closing(sqlite3.connect(uri, uri=True, timeout=0.7)) as src:
        src.execute("PRAGMA query_only=ON")
        src.execute("BEGIN")
        tid_row = src.execute("SELECT trajectory_id FROM trajectory_meta LIMIT 1").fetchone()
        trajectory = tid_row[0] if tid_row and tid_row[0] else path.stem
        project = ag_project(src)
        times, steps, requests = {}, {}, {}
        for idx, blob in src.execute("SELECT idx,metadata FROM steps WHERE metadata IS NOT NULL"):
            try:
                m = fields(blob, {1, 9})
                if 1 in m:
                    times[idx] = ag_timestamp(m[1])
                u = ag_usage(m[9]) if 9 in m else None
                if u:
                    steps[idx] = u
                    if u[0]:
                        requests[u[0]] = idx
            except (ValueError, TypeError):
                issue("steps", idx, "步骤统计或时间字段无效")
                skipped += 1
        names, extra, linked = {}, [], {}
        for idx, blob in src.execute("SELECT idx,data FROM gen_metadata WHERE data IS NOT NULL"):
            try:
                outer = fields(blob, {1, 2})
                if 1 not in outer:
                    continue
                gen = fields(outer[1], {4, 19})
                usage = ag_usage(gen[4]) if 4 in gen else None
                if not usage:
                    continue
                rid, code, counts = usage
                name = ag_model(gen.get(19))
                indices = []
                packed = outer.get(2, b"")
                if isinstance(packed, int):
                    indices.append(packed)
                else:
                    pos = 0
                    while pos < len(packed):
                        index, pos = varint(packed, pos)
                        indices.append(index)
                match = requests.get(rid)
                if match is None:
                    eligible = [
                        i for i in set(indices) if i in steps and (not rid or not steps[i][0])
                    ]
                    matches = [i for i in eligible if steps[i][1:] == (code, counts)]
                    if len(matches) == 1:
                        match = matches[0]
                    elif eligible:
                        issue("gen_metadata", idx, "无法唯一对应已有步骤")
                        # Without a common request ID this cannot be proven to be a separate call.
                        skipped += 1
                        continue
                if match is not None:
                    if rid:
                        linked[match] = rid
                    if times.get(match):
                        extra.append(
                            (
                                times[match],
                                (rid or steps[match][0], code, counts),
                                name,
                                ("step", match),
                                idx,
                                "AGY · gen_metadata",
                            )
                        )
                    if steps[match][1:] == (code, counts) and name:
                        names[match] = name
                    continue
                when = next((times[i] for i in indices if times.get(i)), None)
                if when:
                    extra.append(
                        (when, usage, name, ("generation", idx), idx, "AGY · gen_metadata")
                    )
                else:
                    issue("gen_metadata", idx, "关联步骤缺少可核实时间")
                    skipped += 1
            except (ValueError, TypeError):
                issue("gen_metadata", idx, "生成统计字段无效")
                skipped += 1
        records = []
        for idx, usage in steps.items():
            if times.get(idx):
                extra.append(
                    (
                        times[idx],
                        (usage[0] or linked.get(idx), *usage[1:]),
                        names.get(idx),
                        ("step", idx),
                        idx,
                        "AGY · steps.metadata",
                    )
                )
            else:
                issue("steps", idx, "调用缺少可核实时间")
                skipped += 1
        extra.sort(key=lambda row: row[-1] != "AGY · steps.metadata")
        for ts, (rid, code, counts), name, key, slot, source in extra:
            anchor = digest("agy-local", trajectory, *key)
            uid = digest("agy-request", rid) if rid else anchor
            if anchors is not None:
                anchors.append((anchor, uid if rid else None, slot))
            model = name or (f"AGY #{code} · 型号未确认" if code else "AGY · 型号未记录")
            records.append(
                event(uid, ts, entry, model_vendor(name), model, project, source, counts)
            )
    return records, skipped, None


# Only a positively identified non-usage envelope can take the short path.
# Reordered and future envelopes fall through to the general structural decoder.
NON_USAGE = re.compile(
    rb'^\s*\{\s*(?:"(?:timestamp|ordinal)"\s*:\s*(?:"[^"]*"|[0-9]+)\s*,\s*)*"type"\s*:\s*"(?:response_item|compacted)"'
)


def read_codex(path, saved=None, issues=None):
    issues = issues if issues is not None else []

    def issue(offset, reason):
        if len(issues) < 30:
            issues.append(f"字节 {offset} · {reason}")

    compressed = path.suffix == ".zst"
    if compressed:
        from compression import zstd

        stream = io.BufferedReader(zstd.open(path, "rb"), 256 * 1024)
    else:
        stream = path.open("rb", buffering=256 * 1024)
    state = dict(
        session=path.name.split(".jsonl")[0],
        turn=None,
        model=None,
        provider=None,
        project=UNKNOWN,
        last=None,
        birth=None,
        offset=0,
        marker=None,
    )
    records, skipped, resumed = [], 0, False
    with stream:
        if saved and not compressed and 0 < saved["offset"] < path.stat().st_size:
            offset = saved["offset"]
            prefix_ok = digest(stream.read(min(offset, 512)).hex()) == saved.get("prefix")
            stream.seek(max(0, offset - 256))
            if prefix_ok and digest(stream.read(min(256, offset)).hex()) == saved.get("marker"):
                state.update(saved)
                if isinstance(state["project"], list):
                    state["project"] = ProjectRef(*state["project"])
                resumed = True
            else:
                stream.seek(0)
        while True:
            start = stream.tell()
            data = stream.readline(64 * 1024)
            if not data:
                break
            if NON_USAGE.match(data[:512]):
                while data and not data.endswith(b"\n"):
                    data = stream.readline(256 * 1024)
                if not data:
                    if not compressed:
                        stream.seek(start)
                    break
                continue
            if not data.endswith(b"\n"):
                data += stream.readline(16 * 1024 * 1024 + 1 - len(data))
            candidate = any(
                key in data
                for key in (
                    b'"session_meta"',
                    b'"turn_context"',
                    b'"token_count"',
                    b'"last_token_usage"',
                    b'"total_token_usage"',
                )
            )
            if not data.endswith(b"\n"):
                chunk = data
                while len(chunk) and not chunk.endswith(b"\n"):
                    chunk = stream.readline(256 * 1024)
                    candidate |= b'"token_count"' in chunk or b'"last_token_usage"' in chunk
                if not chunk:
                    if not compressed:
                        stream.seek(start)
                    break
                if candidate:
                    skipped += 1
                    issue(start, "疑似用量行超过 16 MiB")
                continue
            if not candidate:
                continue
            try:
                obj = json.loads(data)
                tag = obj.get("type")
                if tag not in ("session_meta", "turn_context", "event_msg"):
                    payload = obj.get("payload")
                    if (
                        tag == "token_count"
                        or isinstance(payload, dict)
                        and payload.get("type") == "token_count"
                    ):
                        skipped += 1
                        issue(start, "疑似用量事件的外层类型未识别")
                    continue
                p = obj["payload"]
                if tag == "event_msg" and p.get("type") != "token_count":
                    if isinstance(p.get("info"), dict) and (
                        "last_token_usage" in p["info"] or "total_token_usage" in p["info"]
                    ):
                        skipped += 1
                        issue(start, "疑似用量事件类型未识别")
                    continue
                if tag == "session_meta":
                    state.update(
                        session=p.get("id") or p.get("session_id") or state["session"],
                        provider=p.get("model_provider"),
                        project=project_name(p.get("cwd")),
                        birth=stamp(p.get("timestamp") or obj["timestamp"]),
                    )
                elif tag == "turn_context":
                    state.update(
                        turn=p.get("turn_id"),
                        model=p.get("model"),
                        project=project_name(p.get("cwd")) if p.get("cwd") else state["project"],
                    )
                else:
                    ts = stamp(obj["timestamp"])
                    inherited = state["birth"] and ts < state["birth"]
                    if inherited and not state["turn"]:
                        issue(start, "分叉历史缺少可验证的原始回合身份")
                        skipped += 1
                        continue
                    info = p.get("info")
                    if not isinstance(info, dict):
                        if info is not None or "rate_limits" not in p:
                            issue(start, "token_count 缺少有效 info")
                            skipped += 1
                        # A rate-limit heartbeat does not report usage.
                        continue
                    cum = info.get("total_token_usage") or {}
                    last = info.get("last_token_usage")
                    cumulative = cum.get("total_tokens")
                    if cumulative is not None and cumulative == state["last"]:
                        continue
                    if not isinstance(last, dict) or not last:
                        issue(start, "缺少单次 last_token_usage")
                        skipped += 1
                        continue
                    try:
                        counts = counters(
                            last.get("total_tokens"),
                            last.get("input_tokens"),
                            last.get("output_tokens"),
                            last.get("cached_input_tokens"),
                            last.get("cache_write_input_tokens"),
                            last.get("reasoning_output_tokens"),
                        )
                    except ValueError as exc:
                        # counters() reports only our accounting rules, never the original line.
                        issue(start, str(exc))
                        skipped += 1
                        continue
                    state["last"] = cumulative
                    model = p.get("model") or state["model"] or "Codex · 型号未记录"
                    vendor = model_vendor(
                        model, "OpenAI" if state["provider"] == "openai" else UNKNOWN_VENDOR
                    )
                    uid = digest(
                        "codex-event", state["turn"] or state["session"], ts, cumulative, counts
                    )
                    records.append(
                        event(
                            uid,
                            ts,
                            "codex",
                            vendor,
                            model,
                            state["project"],
                            "Codex · token_count",
                            counts,
                        )
                    )
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                issue(start, "统计字段、JSON 或时间格式无效 · " + type(exc).__name__)
                skipped += 1
        if not compressed:
            state["offset"] = stream.tell()
            stream.seek(0)
            state["prefix"] = digest(stream.read(min(state["offset"], 512)).hex())
            stream.seek(max(0, state["offset"] - 256))
            state["marker"] = digest(stream.read(min(256, state["offset"])).hex())
            state["_resumed"] = resumed
    return records, skipped, None if compressed else state


def signature(path):
    stat = path.stat()
    value = [4, stat.st_ino, stat.st_size, stat.st_mtime_ns]
    with path.open("rb") as src:
        sample = src.read(512)
        src.seek(max(0, stat.st_size - 512))
        value.append(hashlib.blake2s(sample + src.read(512), digest_size=12).hexdigest())
    if path.suffix == ".db":
        wal = Path(str(path) + "-wal")
        if wal.exists():
            w = wal.stat()
            value.extend([w.st_size, w.st_mtime_ns])
    return json.dumps(value)


def sync(file, progress=lambda **kwargs: None, roots=None):
    report = {}
    with closing(connect(file)) as db:
        selected = roots or locations()
        if roots is None and codex_search.setting(db, "codex_only", False):
            selected = {"codex": selected["codex"]}
        for entry, dirs in selected.items():
            present = False
            paths, details = [], []
            added = skipped = failures = readable = 0
            stats = {"enriched": 0, "conflicts": 0}
            if entry == "codex" and roots is None:
                paths, present = codex_search.search(db)
                for warning in (codex_search.setting(db, "last_search") or {}).get("warnings", []):
                    if (
                        "未能读取" in warning
                        or "暂不可读" in warning
                        or "不可用" in warning
                        or "无法核实" in warning
                    ):
                        details.append(["搜寻目录", warning])
                        failures += 1
                dirs = []
            for directory in dirs:
                try:
                    with os.scandir(directory) as listing:
                        present = True
                        if entry != "codex":
                            paths.extend(
                                Path(p.path)
                                for p in listing
                                if p.name.endswith(".db") and p.is_file(follow_symlinks=False)
                            )
                        else:

                            def failed(exc):
                                details.append(
                                    [Path(exc.filename or directory).name, "目录访问失败"]
                                )

                            paths.extend(
                                base / name
                                for base, _, names in directory.walk(on_error=failed)
                                for name in names
                                if name.startswith("rollout-")
                                and name.endswith((".jsonl", ".jsonl.zst"))
                            )
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    details.append([directory.name, type(exc).__name__ + " · 目录访问失败"])
            failures = len(details)
            last_read = None
            paths = set(paths)
            discovered = len(paths)
            available = []
            for path in paths:
                try:
                    available.append((path.stat().st_mtime_ns, path))
                except OSError as exc:
                    failures += 1
                    details.append([path.name, type(exc).__name__])
            paths = [p for _, p in sorted(available, reverse=True)]
            for n, path in enumerate(paths, 1):
                progress(entry=entry, done=n - 1, files=len(paths))
                try:
                    key, sig = (
                        digest(
                            "file-v2" if entry == "codex" else "file-v1", entry, str(path.resolve())
                        ),
                        signature(path),
                    )
                    old = db.execute("SELECT * FROM files WHERE id=?", (key,)).fetchone()
                    if entry == "codex" and old and old["skipped"]:
                        try:
                            diagnostics = json.loads(old["cursor"] or "{}")
                        except (TypeError, ValueError):
                            diagnostics = None
                        if not isinstance(diagnostics, dict) or not diagnostics.get(
                            "counter_diagnostics"
                        ):
                            # Revisit skipped records once, including in unchanged compressed archives.
                            old = None
                    if old and old["sig"] == sig:
                        readable += 1
                        skipped += old["skipped"]
                        if old["skipped"]:
                            details.extend(
                                [path.name, message]
                                for message in (json.loads(old["cursor"] or "{}") or {}).get(
                                    "issues", []
                                )
                            )
                            details.append(
                                [
                                    path.name,
                                    f"有 {old['skipped']} 条未纳入，请核对日志版本、分叉身份或统计字段",
                                ]
                            )
                        continue
                    saved = None
                    if old and old["cursor"] and entry == "codex" and path.suffix != ".zst":
                        try:
                            prev, current = json.loads(old["sig"]), json.loads(sig)
                            if prev[:2] == current[:2] and current[2] > prev[2]:
                                candidate = json.loads(old["cursor"])
                                if (
                                    isinstance(candidate, dict)
                                    and type(candidate.get("offset")) is int
                                ):
                                    saved = candidate
                        except (ValueError, TypeError, IndexError):
                            pass  # Unrecognized older cursor: replay the source using stable event IDs.
                    issues = []
                    if entry == "codex":
                        rows, missed, cursor = read_codex(path, saved, issues)
                        if cursor and cursor.pop("_resumed", False):
                            missed += old["skipped"]
                            issues = (saved.get("issues") or []) + issues
                    else:
                        anchors = []
                        rows, missed, cursor = read_agy(path, entry, anchors, issues)
                    if issues:
                        cursor = cursor or {}
                        cursor["issues"] = issues[:30]
                    if entry == "codex" and missed:
                        cursor = cursor or {}
                        cursor["counter_diagnostics"] = 1
                    details.extend([path.name, message] for message in issues[:30])
                    with db:
                        if entry != "codex":
                            from .evidence import identify_agy

                            if not db.in_transaction:
                                db.execute("BEGIN IMMEDIATE")
                            slots = []
                            rows, uncertain = identify_agy(db, rows, anchors, slots)
                            origins = [f"{key}:{slot}" for slot in slots]
                            missed += uncertain
                        added += ingest(db, rows, key if entry == "codex" else origins, stats)
                        db.execute(
                            "INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                            (key, sig, missed, json.dumps(cursor) if cursor else None),
                        )
                    skipped += missed
                    readable += 1
                    last_read = datetime.now(timezone.utc).isoformat()
                    if missed:
                        details.append(
                            [path.name, f"{missed} 条未纳入：统计字段、分叉身份或时间无法核实"]
                        )
                except (
                    sqlite3.Error,
                    OSError,
                    ValueError,
                    EOFError,
                    ImportError,
                    ZstdError,
                ) as exc:
                    failures += 1
                    details.append([path.name, type(exc).__name__ + " · 无法读取来源文件"])
            checked = datetime.now(timezone.utc).isoformat()
            with db:
                db.execute(
                    """INSERT INTO health VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(entry) DO UPDATE SET
                    last_read=coalesce(excluded.last_read,health.last_read), readable=excluded.readable, added=excluded.added,
                    enriched=excluded.enriched,conflicts=excluded.conflicts,details=excluded.details,ever_present=max(health.ever_present,excluded.ever_present)""",
                    (
                        entry,
                        last_read,
                        readable,
                        added,
                        stats["enriched"],
                        stats["conflicts"],
                        json.dumps(details, ensure_ascii=False),
                        int(present),
                    ),
                )
                db.execute(
                    "INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?,?)",
                    (entry, checked, discovered, failures, skipped, int(present)),
                )
            report[entry] = dict(
                **stats,
                readable=readable,
                files=discovered,
                added=added,
                failures=failures,
                skipped=skipped,
            )
    return report
