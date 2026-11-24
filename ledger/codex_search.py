"""Discover logs for one Codex installation and preserve independent index facts."""
import json
import os
import re
import sqlite3
from compression import zstd
from contextlib import closing
from pathlib import Path

from .core import digest


THREAD = re.compile(r"([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl(?:\.zst)?$", re.I)


def matches_session(path, uid):
    opener = zstd.open if path.suffix == ".zst" else open
    with opener(path, "rb") as stream:
        line = stream.readline(16 * 1024 * 1024 + 1)
    if not line.endswith(b"\n"):
        return False
    obj = json.loads(line)
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return False
    payload = obj.get("payload")
    return isinstance(payload, dict) and str(payload.get("id") or payload.get("session_id")).lower() == uid


def setting(db, key, default=None):
    row = db.execute("SELECT value FROM codex_search_settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def save(db, key, value):
    db.execute("INSERT OR REPLACE INTO codex_search_settings VALUES (?,?)", (key, json.dumps(value)))


def current_root(db):
    return Path(setting(db, "home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()


def other_roots(root):
    others = set()
    for parent in {root.parent, Path.home()}:
        for candidate in parent.glob(".codex*"):
            if candidate.is_dir() and candidate.resolve() != root and any(
                (candidate / name).is_dir() for name in ("sessions", "archived_sessions")
            ):
                others.add(candidate.resolve())
    return sorted(others)


def set_backup(db, value):
    if not isinstance(value, str):
        raise ValueError("请输入本机备份目录")
    value = value.strip()
    if value:
        path = Path(value)
        if not path.is_absolute() or str(path).startswith(("//", "\\\\")) or not path.is_dir():
            raise ValueError("请选择已存在的本地绝对目录")
        path = path.resolve()
        root = current_root(db)
        if any(path.is_relative_to(other) for other in other_roots(root)):
            raise ValueError("这个目录属于另一套 Codex 配置")
        value = str(path)
    with db:
        save(db, "backup", value)


def read_index(root):
    versions = []
    for path in root.glob("state_*.sqlite"):
        match = re.fullmatch(r"state_(\d+)\.sqlite", path.name)
        if match and path.resolve().parent == root:
            versions.append((int(match[1]), path))
    if not versions:
        return None, "未找到线程索引"
    path = max(versions)[1]
    try:
        wal = Path(str(path) + "-wal")
        live = wal.exists() and wal.stat().st_size > 0
        if live and not Path(str(path) + "-shm").exists():
            return None, "索引正在更新，下次重试"
        uri = path.as_uri() + ("?mode=ro" if live else "?mode=ro&immutable=1")
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as src:
            rows = src.execute("SELECT id,rollout_path,tokens_used FROM threads").fetchall()
        return {uid.lower(): (location, total) for uid, location, total in rows if isinstance(uid, str)}, None
    except (OSError, sqlite3.Error):
        return None, "索引暂不可读"


def search(db):
    root = current_root(db)
    if root.is_dir():
        with db:
            db.execute("INSERT OR IGNORE INTO codex_search_settings VALUES ('home',?)", (json.dumps(str(root)),))
        root = current_root(db)
    index, warning = read_index(root)
    backup = setting(db, "backup")
    other = other_roots(root)
    paths, located = set(), set()
    warnings = []
    if warning:
        warnings.append(warning)
    history = {r[0] for r in db.execute("SELECT id FROM codex_thread_index")} if backup else set()
    def add(p, trusted=False, expected=None):
        if not p.name.endswith((".jsonl", ".jsonl.zst")) or (trusted and not p.name.startswith("rollout-")):
            return
        match = THREAD.search(p.name)
        uid = expected or (match[1].lower() if match else None)
        if not trusted and (not uid or (uid not in (index or {}) and digest("codex-thread", uid) not in history)):
            return
        if p in paths or p.is_symlink():
            return
        if not trusted:
            try:
                verified = matches_session(p, uid)
            except (ValueError, OSError, EOFError, zstd.ZstdError):
                verified = False
            if not verified:
                if "部分补查文件无法核实会话身份" not in warnings:
                    warnings.append("部分补查文件无法核实会话身份")
                return
        paths.add(p)
        if uid:
            located.add(uid)
    def walk(folder, trusted=False):
        try:
            folder.stat()
        except FileNotFoundError:
            return
        except OSError:
            if "部分目录未能读取" not in warnings:
                warnings.append("部分目录未能读取")
            return
        if not folder.is_dir() or folder.is_symlink() or folder.is_junction():
            return
        def onerror(_):
            if "部分目录未能读取" not in warnings:
                warnings.append("部分目录未能读取")
        for base, dirs, names in os.walk(folder, onerror=onerror):
            base = Path(base)
            dirs[:] = [d for d in dirs if not (base/d).is_junction() and not (base/d).is_symlink() and base/d not in other]
            for name in names:
                add(base/name, trusted)
    for name in ("sessions", "archived_sessions"):
        walk(root/name, True)
    for uid, (location, _) in (index or {}).items():
        if not location:
            continue
        p = Path(location)
        if not p.is_absolute():
            continue
        p = p.resolve()
        if not p.is_relative_to(root) or any(p.is_relative_to(x) for x in other):
            continue
        for candidate in (p, Path(str(p)+".zst")):
            if candidate.is_file():
                add(candidate, expected=uid)
    for name in ("backups", "rollout-migrations"):
        walk(root/name)
    if backup:
        folder = Path(backup)
        if not folder.is_dir() or any(folder.resolve().is_relative_to(x) for x in other):
            warnings.append("备份目录不可用")
        else:
            walk(folder)
    with db:
        if index is not None:
            db.executemany("""INSERT INTO codex_thread_index(id,total) VALUES (?,?)
                ON CONFLICT(id) DO UPDATE SET total=coalesce(excluded.total,codex_thread_index.total)""",
                ((digest("codex-thread", uid), total if isinstance(total,int) and total>=0 else None)
                 for uid, (_, total) in index.items()))
        db.execute("UPDATE codex_thread_index SET located=0 WHERE located<>0")
        db.executemany("UPDATE codex_thread_index SET located=1 WHERE id=?",
                       ((digest("codex-thread", uid),) for uid in located))
        save(db, "last_search", {"others":[str(p) for p in other], "warnings":warnings,
                                 "checked_backup":backup or ""})
    return sorted(paths), bool(paths) or any((root/name).is_dir() for name in ("sessions","archived_sessions"))


def status(db):
    last = setting(db, "last_search")
    missing, known, total = db.execute("""SELECT count(*),count(total),sum(total)
        FROM codex_thread_index WHERE located=0""").fetchone()
    threads, counted, index_total = db.execute(
        "SELECT count(*),count(total),sum(total) FROM codex_thread_index"
    ).fetchone()
    return dict(home=str(current_root(db)), backup=setting(db,"backup",""), last=last,
                missing=missing, known=known, total=total, threads=threads,
                counted=counted, index_total=index_total)
