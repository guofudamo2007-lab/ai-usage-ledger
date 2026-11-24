use crate::db::*;
use anyhow::{ensure, Context, Result};
use chrono::{DateTime, Datelike, Days, NaiveDate, TimeZone, Utc};
use chrono_tz::Tz;
use fancy_regex::Regex;
use rusqlite::{params_from_iter, types::Value as Sql, Connection};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, HashMap},
    sync::LazyLock,
};

pub const DIMS: [&str; 8] = [
    "entry", "vendor", "model", "project", "source", "account", "task", "uid",
];
const KEYS: [&str; 7] = [
    "total",
    "input",
    "output",
    "cache_read",
    "cache_write",
    "reasoning",
    "net",
];
pub fn arg<'a>(q: &'a Q, k: &str, d: &'a str) -> &'a str {
    q.get(k).map(String::as_str).unwrap_or(d)
}
fn dates(q: &Q) -> Result<(Tz, Option<NaiveDate>, Option<NaiveDate>)> {
    let tz: Tz = arg(q, "tz", "UTC")
        .parse()
        .map_err(|_| anyhow::anyhow!("未知统计时区"))?;
    let today = Utc::now().with_timezone(&tz).date_naive();
    let (a, b) = match arg(q, "period", "week") {
        "today" => (today, today),
        "week" => (today - Days::new(6), today),
        "month" => (today.with_day(1).unwrap(), today),
        "all" => return Ok((tz, None, None)),
        "custom" => (arg(q, "from", "").parse()?, arg(q, "to", "").parse()?),
        _ => anyhow::bail!("未知日期范围"),
    };
    ensure!(
        a <= b && a.year() >= 1970 && b.year() <= 2200,
        "日期范围无效"
    );
    Ok((tz, Some(a), Some(b)))
}
struct RateRules {
    provider: Regex,
    enums: Regex,
    rules: Vec<(Regex, Value)>,
}
static RULES: LazyLock<RateRules> = LazyLock::new(|| {
    let rules=CATALOG["rates"].as_array().unwrap().iter().map(|r|(Regex::new(r[3].as_str().unwrap()).unwrap(),json!({"rule":r[1],"source":"builtin","input":r[4],"cache_read":r[5],"cache_write":r[6],"output":r[7]}))).collect();
    RateRules {
        provider: Regex::new(CATALOG["provider"].as_str().unwrap()).unwrap(),
        enums: Regex::new(CATALOG["enum"].as_str().unwrap()).unwrap(),
        rules,
    }
});
fn resolve(model: &str, overrides: &Value) -> Value {
    if let Some(o) = overrides.get(model) {
        let mut v = o.clone();
        v["source"] = json!("override");
        v["rule"] = json!("自定义单价");
        return v;
    }
    if model.contains("型号未") {
        return Value::Null;
    }
    let name = model.rsplit('/').next().unwrap_or("").trim().to_lowercase();
    let stripped = RULES.provider.replace(&name, "");
    let norm = stripped
        .split(|c: char| c == '_' || c.is_whitespace())
        .filter(|x| !x.is_empty())
        .collect::<Vec<_>>()
        .join("-");
    let norm = RULES.enums.replace(&norm, "");
    RULES
        .rules
        .iter()
        .find(|(r, _)| r.is_match(&norm).unwrap_or(false))
        .map(|(_, v)| v.clone())
        .unwrap_or_default()
}
#[derive(Default)]
struct Agg {
    n: u64,
    sum: [i64; 7],
    known: [u64; 7],
    priced: u64,
    cost: [f64; 6],
    missing_cache: u64,
}
impl Agg {
    fn add(&mut self, u: &Usage, cost: Option<[f64; 6]>) {
        self.n += 1;
        for (i, v) in u
            .fields()
            .into_iter()
            .chain([u.total.zip(u.cache_read).map(|(t, r)| t - r)])
            .enumerate()
        {
            if let Some(x) = v {
                self.sum[i] += x;
                self.known[i] += 1
            }
        }
        if let Some(v) = cost {
            self.priced += 1;
            self.missing_cache += u.cache_read.is_none() as u64;
            for (i, x) in v.into_iter().enumerate() {
                self.cost[i] += x
            }
        }
    }
    fn value(&self) -> Option<f64> {
        (self.priced > 0)
            .then(|| (self.cost[..4].iter().sum::<f64>() * 1e6).round_ties_even() / 1e6)
    }
    fn json(&self) -> Value {
        let mut v = json!({"n":self.n,"value":self.value(),"value_n":self.priced});
        for (i, k) in KEYS.iter().enumerate() {
            v[k] = json!((self.known[i] > 0).then_some(self.sum[i]));
            v[format!("{k}_n")] = json!(self.known[i]);
        }
        v
    }
}
fn cost(u: &Usage, r: &Value) -> Option<[f64; 6]> {
    let (i, o) = (u.input?, u.output?);
    let (read, write) = (u.cache_read.unwrap_or(0), u.cache_write.unwrap_or(0));
    let fresh = (i - read - write).max(0);
    let rate = |k: &str| r[k].as_f64().map(|v| v / 1e6);
    let v = [
        fresh as f64 * rate("input")?,
        read as f64 * rate("cache_read")?,
        write as f64 * rate("cache_write")?,
        o as f64 * rate("output")?,
    ];
    let full = (fresh + read + write) as f64 * rate("input")? + v[3];
    Some([
        v[0],
        v[1],
        v[2],
        v[3],
        full,
        (full - v.iter().sum::<f64>()).max(0.),
    ])
}
fn ranked(rows: &mut [Value], sort: &str) {
    rows.sort_by(|a, b| {
        let av = a[sort].as_f64();
        let bv = b[sort].as_f64();
        bv.partial_cmp(&av)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| {
                ["vendor", "model", "entry", "project"]
                    .iter()
                    .map(|k| a[k].as_str())
                    .cmp(
                        ["vendor", "model", "entry", "project"]
                            .iter()
                            .map(|k| b[k].as_str()),
                    )
            })
    })
}
pub fn record(e: &Event, tz: Tz) -> Value {
    let mut v = serde_json::to_value(e).unwrap();
    v.as_object_mut().unwrap().remove("ts");
    v["timestamp"] = json!(iso(e.ts));
    v["local_time"] = json!(DateTime::from_timestamp_micros(e.ts)
        .unwrap()
        .with_timezone(&tz)
        .to_rfc3339());
    v["entry_name"] = CATALOG["entries"][&e.entry][0].clone();
    v["platform"] = CATALOG["entries"][&e.entry][1].clone();
    v["raw_project"] = json!(e.project);
    v["account"] = json!("账号未确认");
    v["task"] = json!("未关联");
    v["net"] = json!(e.usage.total.zip(e.usage.cache_read).map(|(t, r)| t - r));
    v
}
pub fn view(c: &Connection, q: &Q, export: bool) -> Result<Value> {
    let (tz, mut first, mut last) = dates(q)?;
    let mut terms = vec!["1=1".to_string()];
    let mut params: Vec<Sql> = vec![];
    if let (Some(a), Some(b)) = (first, last) {
        for (day, op) in [(a, ">="), (b + Days::new(1), "<")] {
            let dt = day.and_hms_opt(0, 0, 0).unwrap();
            let boundary = (0..=24)
                .find_map(|h| {
                    tz.from_local_datetime(&(dt + chrono::Duration::hours(h)))
                        .earliest()
                })
                .context("时区边界无效")?;
            terms.push(format!("ts{op}?"));
            params.push(boundary.timestamp_micros().into())
        }
    }
    for dim in DIMS {
        if let Some(v) = q.get(dim).filter(|v| !v.is_empty()) {
            let col = dim;
            terms.push(format!("{col}=?"));
            params.push(v.clone().into())
        }
    }
    if let Some(m) = q.get("missing") {
        ensure!(KEYS.contains(&m.as_str()), "未知用量字段");
        let expr = if m == "net" { "(total-cache_read)" } else { m };
        terms.push(format!("{expr} is null"))
    }
    let sort = arg(q, "sort", "total");
    ensure!(
        ["total", "value", "net", "output"].contains(&sort),
        "不支持的排序"
    );
    let group = arg(q, "group", "model");
    ensure!(
        ["model", "vendor", "entry"].contains(&group),
        "不支持的分组"
    );
    let tx = c.unchecked_transaction()?;
    let revision = setting(&tx, "revision", json!(0))?;
    let overrides = setting(&tx, "prices", json!({}))?;
    let mut st = tx.prepare(&format!(
        "select * from effective where {} order by ts desc,uid",
        terms.join(" and ")
    ))?;
    let mut total = Agg::default();
    let mut buckets: BTreeMap<(&str, String, String), Agg> = BTreeMap::new();
    let mut rates = HashMap::new();
    let (mut lo, mut hi) = (None, None);
    let mut records = vec![];
    let offset = arg(q, "page", "0").parse::<usize>()?.saturating_mul(60);
    let detail = q.get("detail").is_some_and(|v| v == "1");
    for item in st.query_map(params_from_iter(params.iter()), |r| {
        Ok((
            row(r)?,
            r.get::<_, String>(13)?,
            r.get::<_, String>(14)?,
            r.get::<_, String>(15)?,
        ))
    })? {
        let (e, raw, account, task) = item?;
        let make_record = || {
            let mut v = record(&e, tz);
            v["raw_project"] = json!(raw);
            v["account"] = json!(account);
            v["task"] = json!(task);
            v
        };
        if export {
            records.push(make_record());
            continue;
        }
        if detail && (offset..offset.saturating_add(60)).contains(&(total.n as usize)) {
            records.push(make_record())
        }
        let day = DateTime::from_timestamp_micros(e.ts)
            .context("无效事件时间")?
            .with_timezone(&tz)
            .date_naive();
        lo = Some(e.ts);
        hi.get_or_insert(e.ts);
        let r = rates
            .entry(e.model.clone())
            .or_insert_with(|| resolve(&e.model, &overrides));
        let val = cost(&e.usage, r);
        total.add(&e.usage, val);
        for (kind, key, k2) in [
            ("day", day.to_string(), String::new()),
            ("model", e.model.clone(), e.vendor.clone()),
            ("vendor", e.vendor.clone(), String::new()),
            ("entry", e.entry.clone(), String::new()),
            (
                "child",
                serde_json::to_string(&(&e.vendor, &e.model))?,
                e.entry.clone(),
            ),
        ] {
            buckets
                .entry((kind, key, k2))
                .or_default()
                .add(&e.usage, val)
        }
        if detail {
            buckets
                .entry(("project", e.project, String::new()))
                .or_default()
                .add(&e.usage, val)
        }
    }
    if export {
        return Ok(json!(records));
    }
    let mut groups: BTreeMap<&str, Vec<Value>> = BTreeMap::new();
    let mut priced = vec![];
    let mut children: BTreeMap<String, Vec<Value>> = BTreeMap::new();
    for ((kind, k, k2), a) in buckets {
        let mut v = a.json();
        if kind == "child" {
            v["entry"] = json!(k2);
            children.entry(k).or_default().push(v);
            continue;
        }
        v[kind] = json!(k);
        if kind == "model" {
            v["vendor"] = json!(k2);
            let rate = &rates[&k];
            priced.push(json!({"model":k,"vendor":k2,"n":a.n,"priced_n":a.priced,"total_tokens":v["total"],"value":a.value(),"saved":(a.priced>0).then_some(a.cost[5]),"rates":rate,"rule":rate["rule"],"source":rate["source"]}));
        }
        groups.entry(kind).or_default().push(v)
    }
    for m in groups.entry("model").or_default().iter_mut() {
        let mut kids = children
            .remove(&serde_json::to_string(&(&m["vendor"], &m["model"]))?)
            .unwrap_or_default();
        ranked(&mut kids, sort);
        m["children"] = json!(kids)
    }
    for group in ["model", "entry", "vendor", "project"] {
        ranked(groups.entry(group).or_default(), sort)
    }
    ranked(&mut priced, "value");
    if first.is_none() {
        first = lo
            .and_then(DateTime::from_timestamp_micros)
            .map(|t| t.with_timezone(&tz).date_naive());
        last = hi
            .and_then(DateTime::from_timestamp_micros)
            .map(|t| t.with_timezone(&tz).date_naive())
    }
    let mut totals = total.json();
    totals["lo"] = json!(lo);
    totals["hi"] = json!(hi);
    let cost_key = |i: usize| (total.priced > 0).then_some(total.cost[i]);
    let mut v = json!({"scope":{"tz":tz.name(),"first":first.map(|d|d.to_string()),"last":last.map(|d|d.to_string())},"revision":revision,"totals":totals,"trend":groups.entry("day").or_default(),"pricing":{"version":CATALOG["version"],"total":total.value(),"input":cost_key(0),"cache_read":cost_key(1),"cache_write":cost_key(2),"output":cost_key(3),"no_cache":cost_key(4),"saved":cost_key(5),"priced_n":total.priced,"total_n":total.n,"cr_unknown_n":total.missing_cache,"models":priced}});
    if arg(q, "rankings", "") == "1" {
        v["rankings"] =
            json!({"model":groups["model"],"entry":groups["entry"],"vendor":groups["vendor"]})
    } else {
        v["rank"] = json!(groups[group])
    }
    if detail {
        v["project"] = json!(groups["project"]);
        v["entry"] = json!(groups["entry"]);
        v["records"] = json!(records);
        v["limit"] = json!(60);
        v["page"] = json!(offset / 60)
    }
    Ok(v)
}
