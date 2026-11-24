use anyhow::{ensure, Result};
use chrono::{DateTime, Utc};
use rusqlite::{params, Connection, OptionalExtension, Row};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{path::Path, sync::LazyLock, time::Duration};

pub static CATALOG: LazyLock<Value> =
    LazyLock::new(|| serde_json::from_str(include_str!("../catalog.json")).unwrap());
pub const FIELDS: [&str; 6] = [
    "total",
    "input",
    "output",
    "cache_read",
    "cache_write",
    "reasoning",
];
pub type Q = std::collections::BTreeMap<String, String>;
pub fn hash(v: impl AsRef<[u8]>) -> String {
    format!("{:x}", Sha256::digest(v.as_ref()))
}
pub fn identity(v: impl Serialize) -> String {
    hash(serde_json::to_vec(&v).unwrap())
}
pub fn now() -> String {
    Utc::now().to_rfc3339()
}
pub fn stamp(s: &str) -> Result<i64> {
    let t = DateTime::parse_from_rfc3339(s)?.timestamp_micros();
    ensure!((0..7_290_000_000_000_000).contains(&t), "时间超出范围");
    Ok(t)
}
pub fn iso(t: i64) -> String {
    DateTime::from_timestamp_micros(t)
        .unwrap()
        .to_rfc3339_opts(chrono::SecondsFormat::Micros, true)
}
pub fn string(v: &Value, k: &str) -> String {
    v[k].as_str().unwrap_or("").to_owned()
}
pub fn open(p: &Path) -> Result<Connection> {
    let c = Connection::open(p)?;
    c.busy_timeout(Duration::from_secs(15))?;
    Ok(c)
}
pub fn init(p: &Path) -> Result<()> {
    let c = open(p)?;
    let app: i64 = c.query_row("pragma application_id", [], |r| r.get(0))?;
    let existing: bool = c.query_row(
        "select exists(select 1 from sqlite_master where type='table')",
        [],
        |r| r.get(0),
    )?;
    ensure!(
        !existing || app == 1262838869,
        "该文件不属于 Rust 版，已停止打开"
    );
    c.execute_batch("pragma journal_mode=WAL; begin immediate;
 create table if not exists events(uid text primary key,ts integer not null,entry text not null,vendor text not null,model text not null,project text not null,source text not null,total integer,input integer,output integer,cache_read integer,cache_write integer,reasoning integer);
 create index if not exists event_time on events(ts);
 create table if not exists settings(k text primary key,v text not null);
 insert or ignore into settings values('revision','0');
 create table if not exists files(id text primary key,sig text,skipped integer);
 create table if not exists thread_index(id text primary key,total integer,located integer);
 create table if not exists cursors(id text primary key,payload text not null);
 create table if not exists anchors(anchor text primary key,uid text not null);
 create table if not exists origins(uid text,origin text,payload text,primary key(uid,origin,payload));
 create table if not exists heads(uid text,origin text,evidence integer,primary key(uid,origin));
 create table if not exists conflicts(uid text primary key,reason text);
 create table if not exists projects(id text primary key,label text unique);
 create table if not exists batches(id integer primary key,created text,result text,undone text);
 create table if not exists contributions(batch integer,line integer,uid text,payload text,outcome text,primary key(batch,line));
 create index if not exists contribution_uid on contributions(uid);
 create table if not exists notes(id text primary key,payload text not null);
 create table if not exists original_api(uid text primary key,payload text not null);
 create table if not exists aliases(raw text primary key, logical text not null);
 create table if not exists annotations(uid text primary key, project text, account text, task text);
 create table if not exists changes(stamp text,target text,action text,previous text,value text,reason text);
 create view if not exists effective as select e.uid,e.ts,e.entry,e.vendor,e.model,coalesce(a.project,l.logical,e.project) project,e.source,e.total,e.input,e.output,e.cache_read,e.cache_write,e.reasoning,e.project raw_project,coalesce(a.account,'账号未确认') account,coalesce(a.task,'未关联') task from events e left join annotations a using(uid) left join aliases l on l.raw=e.project;
 pragma application_id=1262838869; commit;")?;
    Ok(())
}
pub fn setting(c: &Connection, k: &str, default: Value) -> Result<Value> {
    let s: Option<String> = c
        .query_row("select v from settings where k=?", [k], |r| r.get(0))
        .optional()?;
    Ok(match s {
        Some(s) => serde_json::from_str(&s)?,
        None => default,
    })
}
pub fn save(c: &Connection, k: &str, v: &Value) -> Result<()> {
    c.execute(
        "insert or replace into settings values(?,?)",
        params![k, v.to_string()],
    )?;
    Ok(())
}
pub fn bump(c: &Connection) -> Result<()> {
    c.execute(
        "update settings set v=cast(v as integer)+1 where k='revision'",
        [],
    )?;
    Ok(())
}
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
pub struct Usage {
    pub total: Option<i64>,
    pub input: Option<i64>,
    pub output: Option<i64>,
    pub cache_read: Option<i64>,
    pub cache_write: Option<i64>,
    pub reasoning: Option<i64>,
}
impl Usage {
    pub fn fields(&self) -> [Option<i64>; 6] {
        [
            self.total,
            self.input,
            self.output,
            self.cache_read,
            self.cache_write,
            self.reasoning,
        ]
    }
    pub fn validate(mut self) -> Result<Self> {
        ensure!(self.fields().iter().any(Option::is_some), "缺少用量");
        ensure!(
            self.fields()
                .into_iter()
                .flatten()
                .all(|x| (0..=9_000_000_000_000).contains(&x)),
            "无效用量"
        );
        if let (Some(i), Some(o)) = (self.input, self.output) {
            ensure!(
                self.total.is_none_or(|t| t == i + o) && i + o <= 9_000_000_000_000,
                "总量与输入输出不符"
            );
            self.total = Some(i + o);
        }
        for (a, b) in [
            (self.cache_read, self.input),
            (self.cache_write, self.input),
            (self.reasoning, self.output),
        ] {
            ensure!(!matches!((a,b),(Some(x),Some(y)) if x>y), "子项超过父项")
        }
        let min_i = [self.input, self.cache_read, self.cache_write]
            .into_iter()
            .flatten()
            .max()
            .unwrap_or(0);
        let min_o = self.output.max(self.reasoning).unwrap_or(0);
        ensure!(
            self.total.is_none_or(|t| t >= min_i + min_o),
            "子项超过总量"
        );
        Ok(self)
    }
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Event {
    pub uid: String,
    pub ts: i64,
    pub entry: String,
    pub vendor: String,
    pub model: String,
    pub project: String,
    pub source: String,
    #[serde(flatten)]
    pub usage: Usage,
}
pub fn row(r: &Row) -> rusqlite::Result<Event> {
    Ok(Event {
        uid: r.get(0)?,
        ts: r.get(1)?,
        entry: r.get(2)?,
        vendor: r.get(3)?,
        model: r.get(4)?,
        project: r.get(5)?,
        source: r.get(6)?,
        usage: Usage {
            total: r.get(7)?,
            input: r.get(8)?,
            output: r.get(9)?,
            cache_read: r.get(10)?,
            cache_write: r.get(11)?,
            reasoning: r.get(12)?,
        },
    })
}
pub fn get(c: &Connection, id: &str) -> Result<Option<Event>> {
    Ok(c.prepare_cached("select * from events where uid=?")?
        .query_row([id], row)
        .optional()?)
}
pub fn put(c: &Connection, e: &Event) -> Result<()> {
    let u = &e.usage;
    c.prepare_cached("insert or replace into events values(?,?,?,?,?,?,?,?,?,?,?,?,?)")?
        .execute(params![
            e.uid,
            e.ts,
            e.entry,
            e.vendor,
            e.model,
            e.project,
            e.source,
            u.total,
            u.input,
            u.output,
            u.cache_read,
            u.cache_write,
            u.reasoning
        ])?;
    Ok(())
}
pub fn merge(a: &Event, b: &Event, mirror: bool) -> Result<Event> {
    let mut out = serde_json::to_value(a)?;
    let inc = serde_json::to_value(b)?;
    for k in [
        "ts",
        "entry",
        "model",
        "vendor",
        "project",
        "total",
        "input",
        "output",
        "cache_read",
        "cache_write",
        "reasoning",
    ] {
        let unknown = |v: &Value| {
            v.is_null()
                || v.as_str()
                    .is_some_and(|s| s == "未归类" || s == "厂商未确认" || s.contains("型号未"))
        };
        if out[k] == inc[k] || unknown(&inc[k]) {
            continue;
        }
        if k == "entry" && mirror {
            out[k] = json!("agy-shared")
        } else if unknown(&out[k]) {
            out[k] = inc[k].clone()
        } else {
            anyhow::bail!("同一请求的 {k} 冲突")
        }
    }
    let mut event: Event = serde_json::from_value(out)?;
    event.usage = event.usage.validate()?;
    Ok(event)
}
pub fn project(c: &Connection, s: &str) -> Result<String> {
    let p = std::path::Path::new(s);
    if !p.is_absolute() && !s.starts_with('/') {
        return Ok("未归类".into());
    }
    let name = s
        .trim_end_matches(['\\', '/'])
        .rsplit(['\\', '/'])
        .next()
        .unwrap_or("");
    if name.is_empty() {
        return Ok("未归类".into());
    }
    let key = identity((
        "workspace",
        if s.starts_with('/') {
            s.to_owned()
        } else {
            s.replace('/', "\\").to_lowercase()
        },
    ));
    if let Some(label) = c
        .query_row("select label from projects where id=?", [&key], |r| {
            r.get(0)
        })
        .optional()?
    {
        return Ok(label);
    }
    let mut label = name.chars().take(160).collect::<String>();
    if c.query_row(
        "select exists(select 1 from projects where label=?)",
        [&label],
        |r| r.get::<_, bool>(0),
    )? {
        label = format!("{label} · {}", &key[..8]);
    }
    c.execute("insert into projects values(?,?)", params![key, label])?;
    Ok(label)
}
pub fn vendor(model: &str, fallback: &str) -> String {
    for (prefix, v) in [
        ("gemini-", "Google"),
        ("claude-", "Anthropic"),
        ("gpt-", "OpenAI"),
        ("codex-", "OpenAI"),
        ("grok-", "xAI"),
        ("kimi-", "Moonshot AI"),
    ] {
        if model.to_lowercase().starts_with(prefix) {
            return v.into();
        }
    }
    fallback.into()
}
