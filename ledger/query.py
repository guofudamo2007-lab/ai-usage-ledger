from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .core import ENTRIES, FIELDS, iso

METRICS = {k: k for k in FIELDS}
METRICS["net"] = "(total - cache_read)"
AGG = ", ".join(f"SUM({expr}) AS {k}, COUNT({expr}) AS {k}_n" for k, expr in METRICS.items())
AGG += ", COUNT(*) AS n"
DIMS = ("entry", "vendor", "model", "project", "source")
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
    sql = "SELECT * FROM events" + where + " ORDER BY ts DESC, uid"
    if page is not None:
        sql += " LIMIT ? OFFSET ?"
        params += [LIMIT, page * LIMIT]
    return (record(r, zone) for r in db.execute(sql, params))


def options(db):
    return {
        k: [r[0] for r in db.execute(f"SELECT DISTINCT {k} FROM events ORDER BY {k}")] for k in DIMS
    }


def revision(db):
    return db.execute("SELECT value FROM usage_revision WHERE id=1").fetchone()[0]


def blank(**labels):
    return {**labels, "n": 0, **dict.fromkeys(METRICS), **{k + "_n": 0 for k in METRICS}}


def add_counts(target, row):
    target["n"] += row["n"]
    for key in METRICS:
        if row[key] is not None:
            target[key] = (target[key] or 0) + row[key]
            target[key + "_n"] += row[key + "_n"]


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


def daily_rows(db, args, zone, first, last, detail):
    dims = "vendor, model, entry" + (", project" if detail else "")
    terms = [f"{k}=?" for k in DIMS if args.get(k)]
    values = [args[k] for k in DIMS if args.get(k)]
    base = f"SELECT {dims}, {AGG} FROM events WHERE "
    if (last - first).days > 730:
        where, params, *_ = scope(args)
        db.create_function(
            "localday", 1, lambda ts: datetime.fromtimestamp(ts / 1e6, zone).date().isoformat()
        )
        return grouped(
            db,
            f"SELECT localday(ts) day, {dims}, {AGG} FROM events{where} GROUP BY day, {dims}",
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
    if group not in dims or sort not in ("total", "output", "net"):
        raise ValueError("未知分组或排序方式")
    detail = args.get("detail") == "1"
    with db:
        db.execute("BEGIN")
        rev = revision(db)
        edges = [
            db.execute(
                f"SELECT ts FROM events{where} ORDER BY ts {direction} LIMIT 1", params
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
        trend = sorted(project_rows(daily, ("day",)), key=lambda r: r["day"])
        for r in trend:
            add_counts(totals, r)
        grain = project_rows(daily, ("vendor", "model", "entry"))
        ranks = {}
        rank_groups = dims if args.get("rankings") == "1" else (group,)
        for name in rank_groups:
            ranks[name] = ordered(project_rows(grain, dims[name]), sort, dims[name])
        if "model" in ranks:
            children = {}
            for child in ordered(grain, sort, ("entry",)):
                children.setdefault((child["vendor"], child["model"]), []).append(child)
            for r in ranks["model"]:
                r["children"] = children[(r["vendor"], r["model"])]
        result = dict(
            revision=rev,
            scope=dict(
                first=str(first) if first else None, last=str(last) if last else None, tz=zone.key
            ),
            totals=totals,
            trend=trend,
        )
        if args.get("rankings") == "1":
            result["rankings"] = ranks
        else:
            result["rank"] = ranks[group]
        if detail:
            result["entry"] = ordered(project_rows(grain, ("entry",)), "total", ("entry",))
            result["project"] = ordered(project_rows(daily, ("project",)), "total", ("project",))
            page = max(0, min(int(args.get("page", 0)), max(0, (totals["n"] - 1) // LIMIT)))
            result.update(records=list(records(db, args, page)), page=page, limit=LIMIT)
    return result


def sources(db):
    stats = {
        r["entry"]: r
        for r in db.execute(
            "SELECT entry, MIN(ts) lo, MAX(ts) hi, COUNT(*) n FROM events GROUP BY entry"
        )
    }
    scans = {r["entry"]: dict(r) for r in db.execute("SELECT * FROM scans")}
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
        result.append(
            dict(
                key=key,
                name=name,
                platform=platform,
                mode=mode,
                method=method,
                n=s["n"] if s else None,
                first=iso(s["lo"]) if s else None,
                last=iso(s["hi"]) if s else None,
                **{k: v for k, v in check.items() if k != "entry"},
                **{k: v for k, v in handwritten.items() if k != "entry"},
            )
        )
    return result
