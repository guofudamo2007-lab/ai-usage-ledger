"""Read Codex subscription windows through the signed-in CLI."""
import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .codex_search import current_root, save, setting


def windows(bucket):
    if not isinstance(bucket, dict) or bucket.get("limitId") not in (None, "codex"):
        return []
    result = []
    for key in ("primary", "secondary"):
        window = bucket.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("usedPercent")
        if type(used) not in (int, float) or not 0 <= used <= 100:
            continue
        mins, reset = window.get("windowDurationMins"), window.get("resetsAt")
        result.append({
            "slot": key, "remaining": round(100 - used, 6),
            "minutes": mins if type(mins) is int and mins > 0 else None,
            "reset": reset if type(reset) is int and 0 < reset < 253402300800 else None,
        })
    return result


def read_live(home):
    if not home.is_dir():
        raise ValueError("当前账本的 Codex 配置目录不可用")
    paths = [p for p in os.get_exec_path() if Path(p).is_absolute()]
    name = "codex.exe" if os.name == "nt" else "codex"
    binary = next((Path(p) / name for p in paths if (Path(p) / name).is_file()), None)
    if not binary and os.environ.get("LOCALAPPDATA"):
        binary = Path(os.environ["LOCALAPPDATA"]) / "Programs/OpenAI/Codex/bin" / name
    if not binary or not binary.is_file():
        raise ValueError("找不到 Codex 命令行，请先安装 Codex CLI")
    env = dict(os.environ, CODEX_HOME=str(home))
    try:
        proc = subprocess.Popen([str(binary), "app-server"], env=env, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        raise ValueError("未能启动 Codex 命令行") from None
    timer = threading.Timer(18, proc.kill)
    timer.start()

    def send(message):
        proc.stdin.write(json.dumps(message).encode() + b"\n")
        proc.stdin.flush()
    try:
        send({"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "meter", "version": "1"}}})
        for raw in proc.stdout:
            message = json.loads(raw)
            if message.get("id") == 1 and "error" in message:
                raise ValueError("当前 Codex 命令行不支持额度查询")
            if message.get("id") == 1 and "result" in message:
                send({"method": "initialized"})
                send({"id": 2, "method": "account/rateLimits/read", "params": None})
            elif message.get("id") == 2:
                result = message.get("result") or {}
                buckets = result.get("rateLimitsByLimitId") or {}
                data = windows(buckets.get("codex") or result.get("rateLimits"))
                if data:
                    return {"at": datetime.now(timezone.utc).isoformat(), "windows": data, "source": "rpc"}
                if "error" in message:
                    raise ValueError("Codex 未能查询额度，请检查当前登录和网络")
                raise ValueError("Codex 未返回此订阅的额度窗口")
    except (OSError, json.JSONDecodeError):
        pass
    finally:
        timer.cancel()
        proc.kill()
        proc.wait()
        proc.stdin.close()
        proc.stdout.close()
    raise ValueError("Codex 额度查询未完成，请稍后重试")


def cached(db):
    saved = setting(db, "quota_snapshot")
    if saved and saved["home"] == str(current_root(db)):
        return saved["snapshot"]


_query_lock = threading.Lock()


def refresh(db, *, force=True):
    with _query_lock:
        previous = cached(db)
        if previous and not force:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(previous["at"])
            if 0 <= age.total_seconds() < 300:
                return previous
        return _refresh(db)


def _refresh(db):
    home = current_root(db)
    snapshot = read_live(home)
    with db:
        db.execute("BEGIN IMMEDIATE")
        previous = cached(db)
        if previous and previous["at"] > snapshot["at"]:
            return previous
        save(db, "quota_snapshot", {"home": str(home), "snapshot": snapshot})
    return snapshot
