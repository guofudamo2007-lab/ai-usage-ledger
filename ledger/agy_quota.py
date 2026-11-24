"""Read the signed-in Antigravity CLI's built-in /usage report."""
import json
import os
import re
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

from .codex_search import save, setting


def parse_report(report):
    if (not isinstance(report, dict) or report.get("status") != "SUCCESS"
            or report.get("num_turns") != 0 or not isinstance(report.get("response"), str)):
        raise ValueError("AGY 未能返回额度报表，请检查 CLI 登录状态")
    windows = []
    for line in report["response"].strip("\r\n").splitlines():
        cells = [s.strip() for s in line.split("\t")]
        if len(cells) not in (3, 4):
            raise ValueError("当前 AGY 报表格式尚未识别")
        group, label, percentage = cells[:3]
        if not group or not label or not re.fullmatch(r"\d{1,3}(?:\.\d+)?%", percentage):
            raise ValueError("当前 AGY 报表缺少可核实的额度字段")
        remaining = float(percentage[:-1])
        if remaining > 100:
            raise ValueError("AGY 剩余比例超出有效范围")
        reset = None
        if len(cells) == 4 and cells[3]:
            try:
                date = datetime.fromisoformat(cells[3])
                if date.tzinfo is None:
                    raise ValueError
                reset = int(date.timestamp())
            except (ValueError, OSError, OverflowError):
                raise ValueError("无法识别 AGY 重置时间") from None
        windows.append({"group": group, "label": label, "remaining": remaining, "reset": reset})
    if not windows:
        raise ValueError("AGY 尚未提供此账号的额度窗口")
    return windows


def read_live(directory):
    name = "agy.exe" if os.name == "nt" else "agy"
    folders = [Path(p) for p in os.get_exec_path() if Path(p).is_absolute()]
    if os.environ.get("LOCALAPPDATA"):
        folders.append(Path(os.environ["LOCALAPPDATA"]) / "agy/bin")
    binary = next((p / name for p in folders if (p / name).is_file()), None)
    if not binary:
        raise ValueError("未检测到 Antigravity CLI。安装并登录 agy 后即可自动读取。")
    directory.mkdir(exist_ok=True)
    try:
        run = subprocess.run([str(binary), "-p", "/usage", "--output-format", "json", "--print-timeout", "20s"],
                             cwd=directory, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=35, check=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        report = json.loads(run.stdout)
    except (ValueError, OSError, subprocess.SubprocessError):
        raise ValueError("AGY 查询未完成，请检查 CLI 登录和网络后重试") from None
    return {"at": datetime.now(timezone.utc).isoformat(), "source": "agy-cli", "windows": parse_report(report)}


_query_lock = threading.Lock()


def snapshot(db, directory, *, refresh=False, force=False):
    if setting(db, "codex_only", False):
        raise ValueError("此账本仅包含所选 Codex 配置")
    if not refresh:
        return setting(db, "agy_quota_snapshot")
    with _query_lock:
        previous = setting(db, "agy_quota_snapshot")
        if previous and not force:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(previous["at"])
            if 0 <= age.total_seconds() < 300:
                return previous
        result = read_live(directory)
        with db:
            save(db, "agy_quota_snapshot", result)
        return result
