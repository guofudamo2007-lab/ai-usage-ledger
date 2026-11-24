import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import pricing
from .core import ENTRIES, FIELDS, iso

METRICS = {k: k for k in FIELDS}
METRICS["net"] = "(total - cache_read)"
AGG = ", ".join(f"SUM({expr}) AS {k}, COUNT({expr}) AS {k}_n" for k, expr in METRICS.items())
AGG += ", COUNT(*) AS n"
PRICING_AGG = (
    ", SUM(CASE WHEN input IS NOT NULL AND output IS NOT NULL "
    "THEN MAX(0, input - COALESCE(cache_read, 0) - COALESCE(cache_write, 0)) END) AS p_fresh"
    ", SUM(CASE WHEN input IS NOT NULL AND output IS NOT NULL THEN COALESCE(cache_read, 0) END) AS p_cr"
    ", SUM(CASE WHEN input IS NOT NULL AND output IS NOT NULL THEN COALESCE(cache_write, 0) END) AS p_cw"
    ", SUM(CASE WHEN input IS NOT NULL AND output IS NOT NULL THEN output END) AS p_out"
    ", COUNT(CASE WHEN input IS NOT NULL AND output IS NOT NULL THEN 1 END) AS p_n"
    ", COUNT(CASE WHEN input IS NOT NULL AND output IS NOT NULL AND cache_read IS NULL THEN 1 END) AS p_cr_unk"
)
COST_PARTS = ("v_in", "v_cr", "v_cw", "v_out", "v_nocache", "v_saved")
DIMS = ("entry", "vendor", "model", "project", "source", "account", "task", "uid")
LIMIT = 60


def scope(args):
    try:
        zone = ZoneInfo(args.get("tz", "UTC"))
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("未知统计时区") from None
    today = datetime.now(zone).date()
    period = args.get("period", "week")
    last = today
    if period == "all":
        first = last = None
    elif period == "today":
        first = today
    elif period == "week":
        first = today - timedelta(days=6)
    elif period == "month":
        first = today.replace(day=1)
    elif period == "custom":
        first, last = (date.fromisoformat(args.get(k, "")) for k in ("from", "to"))
        if first > last or first.year < 1970 or last.year > 2200:
            raise ValueError("请检查起止日期顺序与年份")
    else:
        raise ValueError("未知时间范围")
    terms, params = [], []
    if first:
        terms += ["ts>=?", "ts<?"]
        params += [
            int(datetime.combine(d, time.min, zone).timestamp()) * 1_000_000
            for d in (first, last + timedelta(days=1))
        ]
    for key in DIMS:
        if args.get(key):
            terms.append(f"{key}=?")
            params.append(args[key])
    missing = args.get("missing")
    if missing:
        if missing not in METRICS:
            raise ValueError("未知缺失字段")
        terms.append(f"{METRICS[missing]} IS NULL")
    return (" WHERE " + " AND ".join(terms) if terms else ""), params, zone, first, last


def grouped(db, sql, params):
    return [dict(r) for r in db.execute(sql, params)]


def record(row, zone):
    result = dict(row)
    ts = result.pop("ts")
    result.update(
        timestamp=iso(ts),
        local_time=datetime.fromtimestamp(ts / 1e6, zone).isoformat(timespec="microseconds"),
        platform=ENTRIES[result["entry"]][1],
        entry_name=ENTRIES[result["entry"]][0],
    )
    result["net"] = (
        result["total"] - result["cache_read"]
        if None not in (result["total"], result["cache_read"])
        else None
    )
    return result


def records(db, args, page=None):
    where, params, zone, _, _ = scope(args)
    sql = "SELECT * FROM ledger_events" + where + " ORDER BY ts DESC, uid"
    if page is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [LIMIT, page * LIMIT]
    return (record(r, zone) for r in db.execute(sql, params))


def options(db):
    return {
        k: [r[0] for r in db.execute(f"SELECT DISTINCT {k} FROM ledger_events ORDER BY {k}")]
        for k in DIMS
        if k != "uid"
    }


def revision(db):
    return db.execute("SELECT value FROM usage_revision WHERE id=1").fetchone()[0]


def blank(**labels):
    return {
        **labels,
        "n": 0,
        **dict.fromkeys(METRICS),
        **{k + "_n": 0 for k in METRICS},
        "value": None,
        "value_n": 0,
        **dict.fromkeys(COST_PARTS, 0.0),
        "p_cr_unk": 0,
    }


def add_counts(target, row):
    target["n"] += row["n"]
    for key in METRICS:
        if row[key] is not None:
            target[key] = (target[key] or 0) + row[key]
            target[key + "_n"] += row[key + "_n"]
    if row.get("value") is not None:
        target["value"] = (target["value"] or 0.0) + row["value"]
        target["value_n"] += row["value_n"]
        for key in COST_PARTS:
            target[key] += row[key]
        target["p_cr_unk"] += row["p_cr_unk"]


def project_rows(rows, keys):
    buckets = {}
    for row in rows:
        key = tuple(row[k] for k in keys)
        if key not in buckets:
            buckets[key] = blank(**dict(zip(keys, key, strict=True)))
        add_counts(buckets[key], row)
    return list(buckets.values())


def ordered(rows, metric, keys):
    return sorted(rows, key=lambda r: (r[metric] is None, -(r[metric] or 0), *(r[k] for k in keys)))


def _attach_pricing(rows, overrides):
    rates = {}
    for row in rows:
        model = row["model"]
        if model not in rates:
            rates[model] = pricing.resolve(model, overrides)
        rate = rates[model]
        pn = row.pop("p_n", 0) or 0
        fresh = row.pop("p_fresh", 0) or 0
        cr = row.pop("p_cr", 0) or 0
        cw = row.pop("p_cw", 0) or 0
        out = row.pop("p_out", 0) or 0
        cr_unk = row.pop("p_cr_unk", 0) or 0
        if rate and pn > 0:
            total, v_in, v_cr, v_cw, v_out, v_nc, v_sv = pricing.cost_parts(fresh, cr, cw, out, rate)
            row.update(
                value=total,
                value_n=pn,
                v_in=v_in,
                v_cr=v_cr,
                v_cw=v_cw,
                v_out=v_out,
                v_nocache=v_nc,
                v_saved=v_sv,
                p_cr_unk=cr_unk,
            )
        else:
            row.update(
                value=None,
                value_n=0,
                **dict.fromkeys(COST_PARTS, 0.0),
                p_cr_unk=0,
            )
    return rates


def _clean_row(row):
    cleaned = {k: v for k, v in row.items() if k not in COST_PARTS and k != "p_cr_unk"}
    if cleaned.get("value") is not None:
        cleaned["value"] = round(cleaned["value"], 6)
    return cleaned


def daily_rows(db, args, zone, first, last, detail):
    dims = "vendor, model, entry" + (", project" if detail else "")
    terms = [f"{k}=?" for k in DIMS if args.get(k)]
    values = [args[k] for k in DIMS if args.get(k)]
    if args.get("missing"):
        terms.append(f"{METRICS[args['missing']]} IS NULL")
    base = f"SELECT {dims}, {AGG}{PRICING_AGG} FROM ledger_events WHERE "
    if (last - first).days > 730:
        where, params, *_ = scope(args)
        db.create_function(
            "localday", 1, lambda ts: datetime.fromtimestamp(ts / 1e6, zone).date().isoformat()
        )
        return grouped(
            db,
            f"SELECT localday(ts) day, {dims}, {AGG}{PRICING_AGG} FROM ledger_events{where} GROUP BY day, {dims}",
            params,
        )
    # Range scans use the time index. Local midnight boundaries also cover 23/25-hour days.
    sql = base + " AND ".join(["ts>=?", "ts<?", *terms]) + f" GROUP BY {dims}"

    def midnight(day):
        return int(datetime.combine(day, time.min, zone).timestamp()) * 1_000_000

    result = []
    day, lo = first, midnight(first)
    while day <= last:
        following = day + timedelta(days=1)
        hi = midnight(following)
        result.extend(dict(r, day=day.isoformat()) for r in db.execute(sql, [lo, hi, *values]))
        day, lo = following, hi
    return result


def view(db, args):
    where, params, zone, first, last = scope(args)
    dims = {"model": ("vendor", "model"), "vendor": ("vendor",), "entry": ("entry",)}
    group, sort = args.get("group", "model"), args.get("sort", "total")
    if group not in dims or sort not in ("total", "output", "net", "value"):
        raise ValueError("未知分组或排序方式")
    detail = args.get("detail") == "1"
    overrides = pricing.read_overrides(db)
    with db:
        db.execute("BEGIN")
        rev = revision(db)
        edges = [
            db.execute(
                f"SELECT ts FROM ledger_events{where} ORDER BY ts {direction} LIMIT 1", params
            ).fetchone()
            for direction in ("ASC", "DESC")
        ]
        totals = blank(lo=edges[0][0] if edges[0] else None, hi=edges[1][0] if edges[1] else None)
        daily = []
        if edges[0]:
            start, end = [datetime.fromtimestamp(edge[0] / 1e6, zone).date() for edge in edges]
            if first is None:
                first, last = start, end
            daily = daily_rows(db, args, zone, start, end, detail)
        rates = _attach_pricing(daily, overrides)
        trend = sorted(project_rows(daily, ("day",)), key=lambda r: r["day"])
        for r in trend:
            add_counts(totals, r)
        grain = project_rows(daily, ("vendor", "model", "entry"))
        model_rows = ordered(project_rows(grain, ("vendor", "model")), "value", ("vendor", "model"))
        priced_models = [
            {
                "vendor": r["vendor"],
                "model": r["model"],
                "n": r["n"],
                "priced_n": r["value_n"],
                "total_tokens": r["total"],
                "value": round(r["value"], 6) if r["value"] is not None else None,
                "saved": round(r["v_saved"], 6) if r["value"] is not None else None,
                "source": rates[r["model"]]["source"] if rates.get(r["model"]) else None,
                "rule": rates[r["model"]]["rule"] if rates.get(r["model"]) else None,
                "rates": (
                    {k: rates[r["model"]][k] for k in pricing.RATE_FIELDS}
                    if rates.get(r["model"])
                    else None
                ),
            }
            for r in model_rows
        ]
        pricing_summary = {
            "version": pricing.CATALOG_VERSION,
            "total": round(totals["value"], 6) if totals["value"] is not None else None,
            "input": round(totals["v_in"], 6) if totals["value_n"] else None,
            "cache_read": round(totals["v_cr"], 6) if totals["value_n"] else None,
            "cache_write": round(totals["v_cw"], 6) if totals["value_n"] else None,
            "output": round(totals["v_out"], 6) if totals["value_n"] else None,
            "no_cache": round(totals["v_nocache"], 6) if totals["value_n"] else None,
            "saved": round(totals["v_saved"], 6) if totals["value_n"] else None,
            "priced_n": totals["value_n"],
            "total_n": totals["n"],
            "cr_unknown_n": totals["p_cr_unk"],
            "models": priced_models,
        }
        ranks = {}
        rank_groups = dims if args.get("rankings") == "1" else (group,)
        for name in rank_groups:
            ranks[name] = ordered(project_rows(grain, dims[name]), sort, dims[name])
        if "model" in ranks:
            children = {}
            for child in ordered(grain, sort, ("entry",)):
                children.setdefault((child["vendor"], child["model"]), []).append(_clean_row(child))
            for r in ranks["model"]:
                r["children"] = children[(r["vendor"], r["model"])]
        totals = _clean_row(totals)
        trend = [_clean_row(r) for r in trend]
        for name, rows in ranks.items():
            ranks[name] = [_clean_row(r) for r in rows]
        result = dict(
            revision=rev,
            scope=dict(
                first=str(first) if first else None, last=str(last) if last else None, tz=zone.key
            ),
            totals=totals,
            trend=trend,
            pricing=pricing_summary,
        )
        if args.get("rankings") == "1":
            result["rankings"] = ranks
        else:
            result["rank"] = ranks[group]
        if detail:
            result["entry"] = [_clean_row(r) for r in ordered(project_rows(grain, ("entry",)), "total", ("entry",))]
            result["project"] = [_clean_row(r) for r in ordered(project_rows(daily, ("project",)), "total", ("project",))]
            page = max(0, min(int(args.get("page", 0)), max(0, (totals["n"] - 1) // LIMIT)))
            result.update(records=list(records(db, args, page)), page=page, limit=LIMIT)
    return result


def sources(db):
    missing_logs = db.execute("SELECT count(*) FROM codex_thread_index WHERE located=0").fetchone()[
        0
    ]
    stats = {
        r["entry"]: r
        for r in db.execute(
            "SELECT entry, MIN(ts) lo, MAX(ts) hi, COUNT(*) n FROM ledger_events GROUP BY entry"
        )
    }
    scans = {r["entry"]: dict(r) for r in db.execute("SELECT * FROM scans")}
    health = {r["entry"]: dict(r) for r in db.execute("SELECT * FROM health")}
    mirrors = {
        r["entry"]: r
        for r in db.execute("""SELECT o.entry,count(DISTINCT o.uid) n,min(e.ts) lo,max(e.ts) hi
                FROM observations o JOIN events e USING(uid) GROUP BY o.entry""")
    }
    notes = {
        r["entry"]: dict(r)
        for r in db.execute(
            "SELECT entry,MIN(day) manual_first, MAX(day) manual_last, MAX(updated) manual_updated, COUNT(*) manual_n FROM notes GROUP BY entry"
        )
    }
    result = []
    for key, (name, platform, mode, method) in ENTRIES.items():
        s, check = stats.get(key), scans.get(key, {})
        handwritten = notes.get(key, {})
        h = health.get(key, {})
        m = mirrors.get(key)
        historical = bool(s or m)
        state = None
        if mode == "auto":
            if not check:
                state = "尚未检查"
            elif check["failures"]:
                state = "文件访问失败"
            elif not check["present"]:
                state = "来源失联" if historical or h.get("ever_present") else "尚未安装或配置"
            elif not check["files"]:
                state = "目录存在 · 暂无文件"
            elif check["skipped"] or (key == "codex" and missing_logs):
                state = "部分覆盖"
            else:
                state = "可读取" if historical else "文件可读 · 暂无用量"
        result.append(
            dict(
                key=key,
                health=state,
                historical=historical,
                observed=m["n"] if m else None,
                **{k: v for k, v in h.items() if k not in ("entry", "details")},
                details=json.loads(h.get("details") or "[]"),
                name=name,
                platform=platform,
                mode=mode,
                method=method,
                missing_logs=missing_logs if key == "codex" else None,
                n=s["n"] if s else None,
                first=iso(min(x["lo"] for x in (s, m) if x)) if historical else None,
                last=iso(max(x["hi"] for x in (s, m) if x)) if historical else None,
                **{k: v for k, v in check.items() if k != "entry"},
                **{k: v for k, v in handwritten.items() if k != "entry"},
            )
        )
    return result
