"""Read-only local adapters. Nothing in this module opens a source for writing."""

import io
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


def read_agy(path, entry):
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
                skipped += 1
        names, extra = {}, []
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
                    match = next(
                        (
                            i
                            for i in indices
                            if i in steps
                            and steps[i][1:] == (code, counts)
                            and (not rid or not steps[i][0])
                        ),
                        None,
                    )
                if match is not None:
                    if steps[match][1:] == (code, counts) and name:
                        names[match] = name
                    continue
                when = next((times[i] for i in indices if times.get(i)), None)
                if when:
                    extra.append((when, usage, name, ("generation", idx), "AGY · gen_metadata"))
                else:
                    skipped += 1
            except (ValueError, TypeError):
                skipped += 1
        records = []
        for idx, usage in steps.items():
            if times.get(idx):
                extra.append(
                    (times[idx], usage, names.get(idx), ("step", idx), "AGY · steps.metadata")
                )
            else:
                skipped += 1
        for ts, (rid, code, counts), name, key, source in extra:
            uid = digest("agy-request", rid) if rid else digest("agy-local", trajectory, *key)
            model = name or (f"AGY #{code} · 型号未确认" if code else "AGY · 型号未记录")
            records.append(
                event(uid, ts, entry, model_vendor(name), model, project, source, counts)
            )
    return records, skipped, None


ENVELOPE = re.compile(
    rb'^\s*\{\s*(?:"(?:timestamp|ordinal)"\s*:\s*(?:"[^"]*"|[0-9]+)\s*,\s*)*"type"\s*:\s*"(session_meta|turn_context|event_msg)"'
)
TOKEN_EVENT = re.compile(rb'"payload"\s*:\s*\{\s*"type"\s*:\s*"token_count"')


def read_codex(path, saved=None):
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
            stream.seek(max(0, offset - 256))
            if digest(stream.read(min(256, offset)).hex()) == saved.get("marker"):
                state.update(saved)
                if isinstance(state["project"], list):
                    state["project"] = ProjectRef(*state["project"])
                resumed = True
            else:
                stream.seek(0)
        while True:
            start = stream.tell() if not compressed else 0
            head = stream.readline(64 * 1024)
            if not head:
                break
            match = ENVELOPE.match(head[:180])
            tag = match[1] if match else None
            keep = bool(tag and (tag != b"event_msg" or TOKEN_EVENT.search(head[:512])))
            data = bytearray(head) if keep else None
            while head and not head.endswith(b"\n"):
                head = stream.readline(256 * 1024)
                if data is not None:
                    if len(data) + len(head) <= 16 * 1024 * 1024:
                        data.extend(head)
                    else:
                        data = None
                        skipped += 1
            if not head:
                if not compressed:
                    stream.seek(start)
                break
            if data is None:
                continue
            try:
                obj = json.loads(data)
                p = obj["payload"]
                if tag == b"session_meta":
                    state.update(
                        session=p.get("id") or p.get("session_id") or state["session"],
                        provider=p.get("model_provider"),
                        project=project_name(p.get("cwd")),
                        birth=stamp(p.get("timestamp") or obj["timestamp"]),
                    )
                elif tag == b"turn_context":
                    state.update(
                        turn=p.get("turn_id"),
                        model=p.get("model"),
                        project=project_name(p.get("cwd")) if p.get("cwd") else state["project"],
                    )
                else:
                    ts = stamp(obj["timestamp"])
                    if state["birth"] and ts < state["birth"]:
                        continue
                    info = p.get("info")
                    if not isinstance(info, dict):
                        # A rate-limit heartbeat does not report usage.
                        continue
                    cum = info.get("total_token_usage") or {}
                    last = info.get("last_token_usage")
                    cumulative = cum.get("total_tokens")
                    if cumulative is not None and cumulative == state["last"]:
                        continue
                    if not isinstance(last, dict) or not last:
                        skipped += 1
                        continue
                    counts = counters(
                        last.get("total_tokens"),
                        last.get("input_tokens"),
                        last.get("output_tokens"),
                        last.get("cached_input_tokens"),
                        last.get("cache_write_input_tokens"),
                        last.get("reasoning_output_tokens"),
                    )
                    state["last"] = cumulative
                    if counts["total"] == 0:
                        continue
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
            except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                skipped += 1
        if not compressed:
            state["offset"] = stream.tell()
            stream.seek(max(0, state["offset"] - 256))
            state["marker"] = digest(stream.read(min(256, state["offset"])).hex())
            state["_resumed"] = resumed
    return records, skipped, None if compressed else state


def signature(path):
    stat = path.stat()
    value = [stat.st_size, stat.st_mtime_ns]
    if path.suffix == ".db":
        wal = Path(str(path) + "-wal")
        if wal.exists():
            w = wal.stat()
            value.extend([w.st_size, w.st_mtime_ns])
    return json.dumps(value)


def sync(file, progress=lambda **kwargs: None, roots=None):
    report = {}
    with closing(connect(file)) as db:
        for entry, dirs in (roots or locations()).items():
            present = any(d.is_dir() for d in dirs)
            paths = []
            for directory in dirs:
                if directory.is_dir():
                    if entry == "codex":
                        paths.extend(
                            p
                            for p in directory.rglob("rollout-*.jsonl*")
                            if p.name.endswith((".jsonl", ".jsonl.zst"))
                        )
                    else:
                        paths.extend(directory.glob("*.db"))
            added = skipped = failures = 0
            available = []
            for path in paths:
                try:
                    available.append((path.stat().st_mtime_ns, path))
                except OSError:
                    failures += 1
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
                    if old and old["sig"] == sig:
                        skipped += old["skipped"]
                        continue
                    saved = json.loads(old["cursor"]) if old and old["cursor"] else None
                    if entry == "codex":
                        rows, missed, cursor = read_codex(path, saved)
                        if cursor and cursor.pop("_resumed", False):
                            missed += old["skipped"]
                    else:
                        rows, missed, cursor = read_agy(path, entry)
                    with db:
                        added += ingest(db, rows)
                        db.execute(
                            "INSERT OR REPLACE INTO files VALUES (?,?,?,?)",
                            (key, sig, missed, json.dumps(cursor) if cursor else None),
                        )
                    skipped += missed
                except (sqlite3.Error, OSError, ValueError, EOFError, ImportError, ZstdError):
                    failures += 1
            checked = datetime.now(timezone.utc).isoformat()
            with db:
                db.execute(
                    "INSERT OR REPLACE INTO scans VALUES (?,?,?,?,?,?)",
                    (entry, checked, len(paths), failures, skipped, int(present)),
                )
            report[entry] = dict(files=len(paths), added=added, failures=failures, skipped=skipped)
    return report
