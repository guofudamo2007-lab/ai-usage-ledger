"""Canonical accounting and durable local storage."""

import hashlib
import json
import ntpath
import posixpath
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from datetime import datetime, timezone
from pathlib import PureWindowsPath
from typing import NamedTuple

UNKNOWN = "未归类"
UNKNOWN_VENDOR = "厂商未确认"
FIELDS = ("total", "input", "output", "cache_read", "cache_write", "reasoning")
ENTRIES = {
    "agy": ("Antigravity", "Google", "auto", "本机会话库 · 调用与步骤元数据"),
    "agy-shared": (
        "Antigravity · 多处发现",
        "Google",
        "derived",
        "同一请求出现在多个入口，实际入口未确认",
    ),
    "agy-cli": ("Antigravity CLI", "Google", "auto", "本机 CLI 会话库 · 调用与步骤元数据"),
    "agy-ide": ("Antigravity IDE", "Google", "auto", "本机 IDE 会话库 · 调用与步骤元数据"),
    "codex": ("Codex", "OpenAI", "auto", "本机 rollout · JSONL / Zstandard"),
    "openai-api": (
        "OpenAI API",
        "OpenAI",
        "import",
        "逐请求 Responses / Chat Completions 或标准明细",
    ),
    "gemini-api": ("Gemini API", "Google", "import", "逐请求 GenerateContent 或标准明细"),
    "ai-studio": ("Google AI Studio", "Google", "import", "含请求标识、时间及实测 usage 的导出"),
    "claude-api": ("Claude API", "Anthropic", "import", "逐请求 Messages 或标准明细"),
    "grok-api": ("Grok API", "xAI", "import", "标准逐请求 JSON / CSV"),
    "kimi-api": ("Kimi API", "Moonshot AI", "import", "标准逐请求 JSON / CSV"),
    "chatgpt-web": ("ChatGPT 网页", "OpenAI", "manual", "手填费用、额度或产出"),
    "gemini-web": ("Gemini 网页", "Google", "manual", "手填费用、额度或产出"),
    "claude-web": ("Claude 网页", "Anthropic", "manual", "手填费用、额度或产出"),
    "grok-web": ("Grok 网页", "xAI", "manual", "手填费用、额度或产出"),
    "kimi-web": ("Kimi 网页", "Moonshot AI", "manual", "手填费用、额度或产出"),
    "prism": ("Obsidian Prism 汇总", "Google", "none", "与 AGY 原始记录重叠，仅供外部对账"),
}


def digest(*parts):
    return hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def text_field(value, limit=160):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("文本字段格式或长度无效")
    return value.strip()


class ProjectRef(NamedTuple):
    name: str
    key: str


def project_name(path):
    if not path or not isinstance(path, str):
        return UNKNOWN
    path = re.sub(r"^/([A-Za-z]:/)", r"\1", path).removeprefix("\\\\?\\")
    if PureWindowsPath(path).is_absolute():
        canonical = ntpath.normcase(ntpath.normpath(path))
    elif path.startswith("/"):
        canonical = posixpath.normpath(path)
    else:
        return UNKNOWN
    name = PureWindowsPath(path).name[:160]
    return ProjectRef(name, digest("workspace", canonical)) if name else UNKNOWN


def model_vendor(name, fallback=UNKNOWN_VENDOR):
    if not isinstance(name, str):
        return fallback
    for prefix, vendor in (
        ("gemini-", "Google"),
        ("claude-", "Anthropic"),
        ("gpt-", "OpenAI"),
        ("codex-", "OpenAI"),
        ("grok-", "xAI"),
        ("kimi-", "Moonshot AI"),
    ):
        if name.lower().startswith(prefix):
            return vendor
    if re.fullmatch(r"o[134](?:-.*)?", name):
        return "OpenAI"
    return fallback


def stamp(value):
    if not isinstance(value, str):
        raise ValueError("时间须为含时区的 ISO 8601 字符串")
    d = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if d.utcoffset() is None or not 1970 <= d.year <= 2200:
        raise ValueError("时间须含时区，年份须在 1970 至 2200 之间")
    return round(d.timestamp() * 1_000_000)


def iso(ts):
    return (
        datetime.fromtimestamp(ts / 1_000_000, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def count(value):
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 9_000_000_000_000:
        raise ValueError("Token 字段须为非负整数或 null")
    return value


def counters(
    total=None, input=None, output=None, cache_read=None, cache_write=None, reasoning=None
):
    values = dict(
        zip(FIELDS, map(count, (total, input, output, cache_read, cache_write, reasoning)))
    )
    if input is not None and output is not None:
        if total is not None and total != input + output:
            raise ValueError("输入与输出之和不等于总量")
        values["total"] = count(input + output)
    if not any(value is not None for value in values.values()):
        raise ValueError("缺少实测用量字段")
    for part, whole in (
        (cache_read, input),
        (cache_write, input),
        (reasoning, output),
        (input, values["total"]),
        (output, values["total"]),
        (cache_read, values["total"]),
        (cache_write, values["total"]),
        (reasoning, values["total"]),
    ):
        if part is not None and whole is not None and part > whole:
            raise ValueError("子项超过对应总量")
    if values["total"] is not None:
        min_input = max(input or 0, cache_read or 0, cache_write or 0)
        min_output = max(output or 0, reasoning or 0)
        if min_input + min_output > values["total"]:
            raise ValueError("已知输入、输出子项超过可分配总量")
    return values


def event(uid, ts, entry, vendor, model, project, source, usage):
    return (
        uid,
        ts,
        entry,
        vendor,
        model or "型号未记录",
        project or UNKNOWN,
        source,
        *(usage[k] for k in FIELDS),
    )


def connect(file):
    db = sqlite3.connect(file, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA temp_store=MEMORY")
    return db


def initialize(file):
    file.parent.mkdir(parents=True, exist_ok=True)
    existing = file.exists() and file.stat().st_size > 0
    with closing(connect(file)) as db:
        _initialize(db, file, existing)


def _initialize(db, file, existing):
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version > 4:
        raise ValueError("此账本由更新版本创建，请使用对应版本打开")
    with db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 4:
            return
        if existing:
            folder = file.parent / "backups"
            folder.mkdir(exist_ok=True)
            dest = folder / f"before-v4-{uuid.uuid4().hex}.sqlite3"
            pending = dest.with_suffix(".partial")
            with closing(connect(file)) as src, closing(sqlite3.connect(pending)) as backup:
                src.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("升级备份未通过完整性检查")
            pending.replace(dest)
        ddl = """
            CREATE TABLE IF NOT EXISTS events (
                uid TEXT PRIMARY KEY, ts INTEGER NOT NULL, entry TEXT NOT NULL, vendor TEXT NOT NULL,
                model TEXT NOT NULL, project TEXT NOT NULL, source TEXT NOT NULL,
                total INTEGER, input INTEGER, output INTEGER, cache_read INTEGER, cache_write INTEGER, reasoning INTEGER
            );
            CREATE INDEX IF NOT EXISTS events_time ON events(ts);
            CREATE INDEX IF NOT EXISTS events_entry ON events(entry,ts);
            CREATE INDEX IF NOT EXISTS events_model ON events(vendor,model,ts);
            CREATE TABLE IF NOT EXISTS usage_revision (
                id INTEGER PRIMARY KEY CHECK(id=1), value INTEGER NOT NULL
            );
            INSERT OR IGNORE INTO usage_revision VALUES (1, 0);
            CREATE TABLE IF NOT EXISTS project_roots (id TEXT PRIMARY KEY, label TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS files (
                id TEXT PRIMARY KEY, sig TEXT NOT NULL, skipped INTEGER NOT NULL, cursor TEXT
            );
            CREATE TABLE IF NOT EXISTS scans (
                entry TEXT PRIMARY KEY, checked TEXT, files INTEGER, failures INTEGER, skipped INTEGER, present INTEGER
            );
            CREATE TABLE IF NOT EXISTS codex_search_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS codex_thread_index (
                id TEXT PRIMARY KEY, total INTEGER, located INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS notes (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL, entry TEXT NOT NULL, data TEXT NOT NULL, updated TEXT
            );
        """
        for statement in (ddl + Path(__file__).with_name("schema4.sql").read_text("utf-8")).split(
            ";"
        ):
            if statement.strip():
                db.execute(statement)
        if "updated" not in {r["name"] for r in db.execute("PRAGMA table_info(notes)")}:
            db.execute("ALTER TABLE notes ADD COLUMN updated TEXT")
        # New parser signatures cause a safe replay, while inaccessible file cursors remain available.
        db.execute("PRAGMA user_version=4")


INSERT = (
    "INSERT INTO events VALUES ("
    + ",".join("?" for _ in range(13))
    + ") ON CONFLICT(uid) DO NOTHING"
)


def project_label(db, ref):
    existing = db.execute("SELECT label FROM project_roots WHERE id=?", (ref.key,)).fetchone()
    if existing:
        return existing[0]
    label = ref.name
    suffix = 0
    while (
        db.execute("SELECT 1 FROM project_roots WHERE label=?", (label,)).fetchone()
        or label == UNKNOWN
    ):
        suffix += 1
        label = f"{ref.name} · {ref.key[:8]}" + (f"-{suffix}" if suffix > 1 else "")
    db.execute("INSERT INTO project_roots VALUES (?,?)", (ref.key, label))
    return label


def mark_usage_changes(db, before):
    if db.total_changes > before:
        db.execute("UPDATE usage_revision SET value=value+1 WHERE id=1")


def ingest(db, rows, origin="direct", stats=None):
    if not rows:
        return 0
    # Serialize label allocation with concurrent collectors before looking up names.
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    labels = {
        ref: project_label(db, ref)
        for ref in dict.fromkeys(r[5] for r in rows)
        if isinstance(ref, ProjectRef)
    }
    revised = [
        (labels[r[5]], r[0], r[5].name)
        for r in rows
        if r[5] in labels and labels[r[5]] != r[5].name
    ]
    rows = [(*r[:5], labels.get(r[5], r[5]), *r[6:]) for r in rows]
    before = db.total_changes
    if rows[0][2].startswith("agy"):
        from .evidence import observe

        stats = stats if stats is not None else {"enriched": 0, "conflicts": 0}
        origins = origin if isinstance(origin, list) else [origin] * len(rows)
        added = sum(
            observe(db, r, location, stats) for r, location in zip(rows, origins, strict=True)
        )
        mark_usage_changes(db, before)
        return added
    db.executemany(INSERT, rows)
    added = db.total_changes - before
    db.executemany("UPDATE events SET project=? WHERE uid=? AND project=?", revised)
    mark_usage_changes(db, before)
    return added
