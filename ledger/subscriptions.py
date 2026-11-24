"""Subscription sources independent of the request-level Token ledger."""
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .codex_search import save, setting


# Only these fixed destinations are linked from the ledger.
SERVICES = {
    "copilot": ("GitHub Copilot", "编程", "https://github.com/settings/billing/usage", "个人用量"),
    "grok": ("Grok / SuperGrok", "对话", "https://grok.com/dashboard", "Usage"),
    "minimax": ("MiniMax M Plan", "编程", "https://www.minimax.cn/m-plan", "套餐用量"),
    "claude-code": ("Claude / Claude Code", "对话", "https://claude.ai/settings/usage", "订阅共用额度"),
    "kimi-code": ("Kimi Code", "编程", "https://www.kimi.com/code/console", "会员额度"),
    "chatgpt-web": ("ChatGPT 网页", "对话", "https://chatgpt.com/", "账号内的用量提示"),
    "gemini-web": ("Gemini 网页", "对话", "https://gemini.google.com/app", "设置 → 使用限额"),
    "kimi-web": ("Kimi 网页", "对话", "https://www.kimi.com/", "我的额度"),
    "perplexity": ("Perplexity", "对话", "https://www.perplexity.ai/account", "Settings → Usage"),
    "m365": ("Microsoft 365 Copilot", "对话", "https://account.microsoft.com/services", "服务和订阅 → AI 点数"),
    "doubao": ("豆包", "对话", "https://www.doubao.com/chat/", "积分 / 会员"),
    "gemini-cli": ("Gemini CLI", "编程", "https://geminicli.com/docs/resources/quota-and-pricing/", "在 CLI 输入 /stats model"),
    "cursor": ("Cursor", "编程", "https://cursor.com/dashboard/usage", "个人用量"),
    "glm-plan": ("智谱 GLM Coding Plan", "编程", "https://bigmodel.cn/apikey/coding-plan-per", "个人套餐概览"),
    "bailian": ("阿里云百炼 Coding Plan", "编程", "https://bailian.console.aliyun.com/cn-beijing/subscription/coding-plan", "套餐用量"),
    "kiro": ("Kiro", "编程", "https://kiro.dev/docs/cli/billing/subscription-portal/", "在 CLI 输入 /usage"),
    "trae": ("TRAE 国际版", "编程", "https://www.trae.ai/", "账号订阅用量"),
    "trae-cn": ("TRAE 国内版", "编程", "https://www.trae.cn/", "头像 → 用量管理"),
    "windsurf": ("Windsurf", "编程", "https://windsurf.com/subscription/usage", "订阅用量"),
    "ark-plan": ("火山方舟 Coding Plan", "编程", "https://ark.volcengine.com/region:cn-beijing/openManagement?advancedActiveKey=subscribe", "开通管理 → 套餐用量"),
    "tencent-plan": ("腾讯云 Coding Plan", "编程", "https://console.cloud.tencent.com/tokenhub/codingplan", "TokenHub 套餐用量"),
    "midjourney": ("Midjourney", "创作", "https://www.midjourney.com/account", "Fast GPU 时长"),
    "runway": ("Runway", "创作", "https://app.runwayml.com/", "工作区 → Plans & Billing"),
    "suno": ("Suno", "创作", "https://suno.com/account", "剩余积分"),
    "firefly": ("Adobe Firefly", "创作", "https://account.adobe.com/", "个人资料 → 生成式点数"),
    "elevenlabs": ("ElevenLabs", "创作", "https://elevenlabs.io/app/subscription", "工作区订阅点数"),
    "jimeng": ("即梦 AI", "创作", "https://jimeng.jianying.com/", "积分 / 会员 → 积分详情"),
    "kling": ("可灵 AI", "创作", "https://klingai.kuaishou.com/", "会员灵感值"),
}
IMPORTS = {"claude-code", "kimi-code"}
READERS = ("copilot", "grok", "minimax")
_locks = {name: threading.Lock() for name in (*READERS, *IMPORTS)}


def _binary(name):
    paths = [Path(p) for p in os.get_exec_path() if Path(p).is_absolute()]
    filename = name + ".exe" if os.name == "nt" else name
    return next((str(p / filename) for p in paths if (p / filename).is_file()), None)


def _mmx():
    binary = _binary("mmx")
    if binary:
        return [binary]
    node = _binary("node")
    if node:
        for folder in os.get_exec_path():
            script = Path(folder) / "node_modules/mmx-cli/dist/mmx.mjs"
            if script.is_absolute() and script.is_file():
                return [node, str(script)]
    return []


def grok_log():
    return Path(os.environ.get("GROK_HOME") or Path.home() / ".grok") / "logs/unified.jsonl"


def catalogue(db, directory):
    if setting(db, "codex_only", False):
        return {}
    available = {"copilot": bool(_binary("gh")), "grok": grok_log().is_file(), "minimax": bool(_mmx())}
    available.update({key: (directory / (key + ".json")).is_file() for key in IMPORTS})
    missing = {"minimax": "登录 mmx 可自动查询", "copilot": "登录 GitHub CLI 可自动查询", "grok": "等待本机 Grok 额度记录"}
    return {key: {"name": v[0], "category": v[1], "url": v[2], "hint": missing[key] if key in missing and not available[key] else v[3],
                  "automatic": available.get(key, False), "importable": key in IMPORTS,
                  "saved": bool(setting(db, "subscription:" + key)) if key in _locks else False}
            for key, v in SERVICES.items()}


def _obj(value):
    return value if isinstance(value, dict) else {}


def _percent(value):
    return type(value) in (int, float) and 0 <= value <= 100


def _date(value):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        pass


def _epoch(value):
    if type(value) in (int, float) and 0 < value < 253402300800:
        return int(value)


def _row(label, percentage=None, reset=None, **more):
    return {"label": label, "remaining": percentage, "reset": reset, **more}


def _json_command(command):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=20, check=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return _obj(json.loads(result.stdout))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise ValueError("查询未完成，请检查客户端登录和网络，或从官方入口查看。") from None


def copilot_windows(data):
    labels = {"chat": "对话", "completions": "代码补全", "premium_interactions": "高级交互"}
    rows = []
    reset_date = _date(data.get("quota_reset_date_utc"))
    for key, item in _obj(data.get("quota_snapshots")).items():
        item = _obj(item)
        label = labels.get(key, key)
        if item.get("unlimited") is True:
            rows.append(_row(label, state="不限量"))
        elif item.get("has_quota") is not False and _percent(item.get("percent_remaining")):
            reset = _epoch(item.get("quota_reset_at")) or (int(reset_date.timestamp()) if reset_date else None)
            rows.append(_row(label, item["percent_remaining"], reset))
    return rows


def minimax_windows(data):
    rows = []
    for model in data.get("model_remains") or []:
        model = _obj(model)
        name = model.get("model_name")
        if not isinstance(name, str) or not name:
            continue
        if all(model.get("current_" + p + "_status") == 3 and model.get("current_" + p + "_total_count") == 0
               for p in ("interval", "weekly")):
            continue
        for prefix, label, end in (("interval", "当前窗口", "end_time"), ("weekly", "每周", "weekly_end_time")):
            value = model.get("current_" + prefix + "_remaining_percent")
            if prefix == "weekly" and model.get("current_weekly_status") == 3:
                rows.append(_row(name + " · " + label, state="不限量"))
            elif _percent(value):
                ms = model.get(end)
                reset = _epoch(ms / 1000) if type(ms) in (int, float) else None
                boost = model.get("weekly_boost_permille")
                if prefix == "weekly" and type(boost) is int and boost >= 0:
                    value = round(value * boost / 1000, 4)
                rows.append(_row(name + " · " + label, value, reset))
    return rows


def grok_snapshot(path):
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            end = stream.tell()
            # Walk blocks from the end. Never return or retain unrelated log records.
            suffix = b""
            while end:
                size = min(end, 131072)
                end -= size
                stream.seek(end)
                lines = (stream.read(size) + suffix).split(b"\n")
                suffix = lines.pop(0) if end else b""
                if len(suffix) > 1048576:
                    suffix = b""
                for raw in reversed(lines):
                    if b"billing: fetched credits config" not in raw:
                        continue
                    try:
                        record = _obj(json.loads(raw))
                    except (ValueError, UnicodeError):
                        continue
                    if record.get("msg") != "billing: fetched credits config":
                        continue
                    config = _obj(_obj(record.get("ctx")).get("config"))
                    used, at = config.get("creditUsagePercent"), _date(record.get("ts"))
                    if not _percent(used) or not at:
                        raise ValueError("最新 Grok 额度记录缺少可核实的比例或时间。")
                    period = _obj(config.get("currentPeriod"))
                    reset = _date(period.get("end"))
                    label = "每周额度" if period.get("type") == "USAGE_PERIOD_TYPE_WEEKLY" else "当前周期"
                    return {"source": "client-log", "at": at.isoformat(),
                            "windows": [_row(label, round(100-used, 5), int(reset.timestamp()) if reset else None)]}
    except OSError:
        pass
    raise ValueError("尚无可读的 Grok 额度快照。可在 Grok 中打开 /usage。")


def imported_windows(key, data):
    rows = []
    if key == "claude-code":
        for field, label in (("five_hour", "5 小时"), ("seven_day", "每周")):
            item = _obj(_obj(data.get("rate_limits")).get(field))
            used = item.get("used_percentage")
            if _percent(used):
                rows.append(_row(label, round(100-used, 5), _epoch(item.get("resets_at"))))
    elif key == "kimi-code":
        payload = _obj(data.get("data", data))
        if payload.get("kind") == "ok":
            usages = _obj(_obj(payload.get("quota")).get("usages"))
            for field, label in (("limit5h", "5 小时"), ("limit7d", "每周"), ("monthTotal", "月总额度"), ("monthCode", "月编程额度")):
                item = _obj(usages.get(field))
                used = item.get("usedRatio")
                if type(used) in (float, int) and 0 <= used <= 1:
                    reset = _date(item.get("resetAt"))
                    rows.append(_row(label, round(100*(1-used), 5), int(reset.timestamp()) if reset else None))
    return rows


def handle(db, key, directory, *, write=False, force=False, imported=None):
    if key not in _locks or setting(db, "codex_only", False):
        raise ValueError("此账本未启用该订阅来源")
    cache_key = "subscription:" + key
    if not write:
        return setting(db, cache_key)
    with _locks[key]:
        saved = setting(db, cache_key)
        if imported is not None:
            if key not in IMPORTS or not isinstance(imported, dict):
                raise ValueError("请选择对应厂商的额度 JSON")
            rows = imported_windows(key, imported)
            source = "import"
        elif key in IMPORTS:
            source = "file"
            try:
                with (directory / (key + ".json")).open("rb") as stream:
                    modified = os.fstat(stream.fileno()).st_mtime
                    if saved and saved["source"] == "file" and saved.get("mtime") == modified and not force:
                        return saved
                    raw = stream.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError
                rows = imported_windows(key, _obj(json.loads(raw)))
            except (OSError, ValueError):
                raise ValueError("额度快照文件不可读或格式不符") from None
        else:
            if key not in READERS:
                raise ValueError("此来源需要导入额度 JSON")
            if saved and not force and 0 <= time.time() - saved["checked"] < 300:
                return saved
            source = key
            if key == "grok":
                result = grok_snapshot(grok_log())
            elif key == "copilot" and _binary("gh"):
                rows = copilot_windows(_json_command([_binary("gh"), "api", "--hostname", "github.com",
                    "/copilot_internal/user", "--jq", "{quota_snapshots,quota_reset_date_utc}"]))
            elif key == "minimax" and _mmx():
                rows = minimax_windows(_json_command([*_mmx(), "quota", "show", "--output", "json"]))
            else:
                raise ValueError("本机未找到此订阅的查询客户端")
        if key != "grok":
            if not rows:
                raise ValueError("未找到可核实的订阅额度字段")
            result = {"source": source, "at": datetime.now(timezone.utc).isoformat(), "windows": rows}
            if source == "file":
                result.update(at=datetime.fromtimestamp(modified, timezone.utc).isoformat(), mtime=modified)
        result["checked"] = time.time()
        with db:
            save(db, cache_key, result)
        return result
