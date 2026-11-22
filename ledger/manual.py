import json
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from .core import ENTRIES, text_field


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
        entry not in ENTRIES
        or entry == "prism"
        or kind not in ("media", "subscription", "api_fee", "quota")
    ):
        raise ValueError("请选择入口和记录类型")
    day = date.fromisoformat(item.get("day", "")).isoformat()
    data = {"title": text_field(item.get("title")), "project": text_field(item.get("project"))}
    if kind == "media":
        data["media"] = item.get("media")
        if data["media"] not in ("图像", "视频", "音频"):
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


def list_notes(db):
    return [
        dict(id=r["id"], day=r["day"], kind=r["kind"], entry=r["entry"], **json.loads(r["data"]))
        for r in db.execute("SELECT * FROM notes ORDER BY day DESC, rowid DESC")
    ]
