import json
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from .core import ENTRIES, text_field
from .subscriptions import SERVICES


def decimal_text(value):
    try:
        number = Decimal(str(value))
        if (
            not number.is_finite()
            or number < 0
            or number > 10**15
            or number.as_tuple().exponent < -8
        ):
            raise ValueError
        return format(number, "f")
    except (ValueError, InvalidOperation):
        raise ValueError("请输入有效的非负金额或额度，最多八位小数") from None


def save(db, item):
    kind, entry = item.get("kind"), item.get("entry")
    if (
        (entry not in ENTRIES and not (kind in ("quota", "subscription") and entry in SERVICES))
        or entry == "prism"
        or kind not in ("media", "subscription", "api_fee", "quota")
    ):
        raise ValueError("请选择入口和记录类型")
    day = date.fromisoformat(item.get("day", "")).isoformat()
    data = {"title": text_field(item.get("title")), "project": text_field(item.get("project"))}
    for key in ("account", "plan"):
        data[key] = text_field(item.get(key))
    for key in ("cycle_start", "cycle_end"):
        data[key] = date.fromisoformat(item[key]).isoformat() if item.get(key) else ""
    if (
        bool(data["cycle_start"]) != bool(data["cycle_end"])
        or data["cycle_start"] > data["cycle_end"]
    ):
        raise ValueError("请填写完整且有序的周期起止日期")
    if kind == "media":
        data["media"] = item.get("media")
        if data["media"] not in (
            "图像",
            "视频",
            "音频",
            "完成章节",
            "可用稿件",
            "设计方案",
            "资料整理",
            "已验收代码任务",
        ):
            raise ValueError("请选择产出类型")
        for k in ("produced", "usable", "retries", "minutes"):
            v = item.get(k)
            if v is None and k in ("retries", "minutes"):
                data[k] = None
            elif type(v) is int and 0 <= v <= 100_000_000:
                data[k] = v
            else:
                raise ValueError("数量与时长请填写非负整数")
        if data["usable"] > data["produced"]:
            raise ValueError("可用数量不能超过总产出")
    amount = item.get("amount")
    if amount not in (None, "") or kind != "media":
        data["amount"] = decimal_text(amount)
        if kind == "quota":
            data["unit"] = text_field(item.get("unit"), 24)
            if not data["unit"]:
                raise ValueError("请填写额度单位")
        else:
            currency = text_field(item.get("currency"), 3).upper()
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError("请使用三位币种代码")
            data["currency"] = currency
    uid = str(uuid.UUID(item["id"])) if item.get("id") else str(uuid.uuid4())
    with db:
        db.execute(
            "INSERT OR REPLACE INTO notes(id,kind,day,entry,data,updated) VALUES (?,?,?,?,?,?)",
            (
                uid,
                kind,
                day,
                entry,
                json.dumps(data, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return {"id": uid}


def list_notes(db, args=None):
    args = dict(args or {})
    for key in ("from", "to", "cycle_start", "cycle_end"):
        if args.get(key):
            args[key] = date.fromisoformat(args[key]).isoformat()
    aliases = dict(db.execute("SELECT raw,logical FROM aliases"))
    rows = [
        dict(id=r["id"], day=r["day"], kind=r["kind"], entry=r["entry"], **json.loads(r["data"]))
        for r in db.execute("SELECT * FROM notes ORDER BY day DESC, rowid DESC")
    ]
    for r in rows:
        r["logical_project"] = aliases.get(r.get("project"), r.get("project")) or "未归类"
    if args.get("from") and args.get("to") and args["from"] > args["to"]:
        raise ValueError("起始日期须早于截止日期")
    return [
        r
        for r in rows
        if (not args.get("from") or r["day"] >= args["from"])
        and (not args.get("to") or r["day"] <= args["to"])
        and all(
            not args.get(k) or args[k] == (r.get(field) or fallback)
            for k, field, fallback in (
                ("project", "logical_project", "未归类"),
                ("account", "account", "账号未确认"),
                ("plan", "plan", ""),
                ("cycle_start", "cycle_start", ""),
                ("cycle_end", "cycle_end", ""),
            )
        )
    ]
