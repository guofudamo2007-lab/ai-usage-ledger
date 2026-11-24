use crate::{db::*, manage::prepare_note};
use anyhow::{ensure, Context, Result};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde::Serialize;
use serde_json::{json, Value};
use std::{
    collections::{BTreeSet, HashMap},
    fs,
    io::ErrorKind,
    os::windows::fs::OpenOptionsExt,
    path::{Component, Path, Prefix},
};
use windows_sys::Win32::UI::Controls::Dialogs::*;

pub fn pick() -> Result<Value> {
    let mut path = vec![0u16; 32768];
    let filter: Vec<_> = "SQLite 账本 (*.sqlite3;*.db)\0*.sqlite3;*.db\0所有文件\0*.*\0\0"
        .encode_utf16()
        .collect();
    let title: Vec<_> = "选择原版账本或完整备份\0".encode_utf16().collect();
    let mut dialog = OPENFILENAMEW {
        lStructSize: std::mem::size_of::<OPENFILENAMEW>() as u32,
        lpstrFile: path.as_mut_ptr(),
        nMaxFile: path.len() as u32,
        lpstrFilter: filter.as_ptr(),
        lpstrTitle: title.as_ptr(),
        Flags: OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR | OFN_DONTADDTORECENT,
        ..Default::default()
    };
    if unsafe { GetOpenFileNameW(&mut dialog) } == 0 {
        ensure!(
            unsafe { CommDlgExtendedError() } == 0,
            "无法打开文件选择器，可粘贴完整路径"
        );
        return Ok(Value::Null);
    }
    Ok(json!(String::from_utf16(
        &path[..path.iter().position(|&c| c == 0).unwrap()]
    )?))
}

#[derive(Default, Serialize)]
struct Counts {
    added: usize,
    existing: usize,
    enriched: usize,
    skipped: usize,
}
#[derive(Default, Serialize)]
struct Report {
    usage: Counts,
    notes: Counts,
    prices: Counts,
    details: Vec<String>,
}
impl Report {
    fn detail(&mut self, id: &str, why: impl std::fmt::Display) {
        if self.details.len() < 30 {
            self.details.push(format!("{id} · {why}"));
        }
    }
}

fn is_local(path: &Path) -> bool {
    path.is_absolute()
        && matches!(path.components().next(), Some(Component::Prefix(p)) if matches!(p.kind(),Prefix::Disk(_) | Prefix::VerbatimDisk(_)))
}
fn table(src: &Connection, name: &str) -> Result<bool> {
    Ok(src.query_row("select exists(select 1 from pragma_table_list where schema='main' and name=? and type='table')",[name],|r|r.get(0))?)
}

// Both the original and Rust can first observe a step without its eventual request ID.
type Keys = HashMap<String, Vec<(String, String)>>;
fn keys(src: &Connection) -> Result<Keys> {
    let mut map = Keys::new();
    if table(src, "agy_keys")? {
        let mut q = src.prepare("select uid,anchor,request from agy_keys")?;
        let mut rows = q.query([])?;
        while let Some(r) = rows.next()? {
            map.entry(r.get(0)?)
                .or_default()
                .push((r.get(1)?, r.get(2)?));
        }
    }
    Ok(map)
}

fn target_id(
    src: &Connection,
    c: &Connection,
    id: &str,
    keys: &[(String, String)],
) -> Result<Option<String>> {
    let mut ids = BTreeSet::new();
    let mut add = |sql: &str, key: &str| -> Result<()> {
        let mut st = c.prepare_cached(sql)?;
        for uid in st.query_map([key], |r| r.get::<_, String>(0))? {
            ids.insert(uid?);
        }
        Ok(())
    };
    add("select uid from events where uid=?", id)?;
    add("select uid from anchors where anchor=?", id)?;
    if keys.is_empty() {
        add(
            "select distinct uid from anchors where anchor>=?1||':' and anchor<?1||';' limit 2",
            id,
        )?;
    } else {
        for (anchor, request) in keys {
            // A shared step can represent independent retries. Do not allocate a prior
            // unbound event to whichever request happens to be visited first.
            let unresolved:bool=c.prepare_cached("select exists(select 1 from events where uid=?1) and not exists(select 1 from anchors where anchor>=?1||':' and anchor<?1||';')")?.query_row([anchor],|r|r.get(0))?;
            if unresolved
                && src
                    .prepare_cached(
                        "select exists(select 1 from agy_keys where anchor=? and request<>?)",
                    )?
                    .query_row(params![anchor, request], |r| r.get::<_, bool>(0))?
            {
                return Ok(None);
            }
            if id==anchor && c.prepare_cached("select exists(select 1 from anchors where uid=?1 and anchor>=?1||':' and anchor<?1||';' and anchor<>?1||':'||?2)")?.query_row(params![anchor,request],|r|r.get::<_,bool>(0))? {
                return Ok(None);
            }
            add("select uid from anchors where anchor=?", request)?;
            add("select uid from events where uid=?", request)?;
            // Match an unbound local step only if no other request is known at that step.
            add("select uid from events where uid=?1 and not exists(select 1 from anchors where anchor>=?1||':' and anchor<?1||';')",anchor)?;
        }
    }
    Ok(if ids.len() > 1 {
        None
    } else {
        Some(ids.pop_first().unwrap_or_else(|| id.to_owned()))
    })
}

fn usage(src: &Connection, c: &Connection, modern: bool, report: &mut Report) -> Result<()> {
    let agy = keys(src)?;
    let base="select e.uid,e.ts,e.entry,e.vendor,e.model,e.project,e.source,e.total,e.input,e.output,e.cache_read,e.cache_write,e.reasoning";
    let extra = if modern {
        ",coalesce(a.project,l.logical),a.account,a.task,d.reason from events e left join annotations a using(uid) left join aliases l on l.raw=e.project left join disputes d on d.uid=e.uid and d.resolved is null"
    } else {
        ",null,null,null,null from events e"
    };
    let mut q = src.prepare(&format!("{base}{extra}"))?;
    let mut rows = q.query([])?;
    while let Some(r) = rows.next()? {
        let mut event = row(r)?;
        let original = event.uid.clone();
        if let Some(reason) = r.get::<_, Option<String>>(16)? {
            report.usage.skipped += 1;
            report.detail(&original, reason);
            continue;
        }
        ensure!(
            !original.is_empty() && (0..7_290_000_000_000_000).contains(&event.ts),
            "原版用量身份或时间无效"
        );
        event.usage = match event.usage.clone().validate() {
            Ok(v) => v,
            Err(e) => {
                report.usage.skipped += 1;
                report.detail(&original, e);
                continue;
            }
        };
        let bindings = agy.get(&original).map(Vec::as_slice).unwrap_or(&[]);
        let is_agy = event.entry == "agy" || event.entry.starts_with("agy-");
        if is_agy {
            let Some(target) = target_id(src, c, &original, bindings)? else {
                report.usage.skipped += 1;
                report.detail(&original, "AGY 请求和步骤对应多条记录");
                continue;
            };
            event.uid = target;
        }
        let prior = get(c, &event.uid)?;
        let effective = if let Some(p) = prior.as_ref() {
            if p == &event {
                p.clone()
            } else {
                match merge(
                    p,
                    &event,
                    is_agy && (p.entry == "agy" || p.entry.starts_with("agy-")),
                ) {
                    Ok(v) => v,
                    Err(e) => {
                        report.usage.skipped += 1;
                        report.detail(&original, e);
                        continue;
                    }
                }
            }
        } else {
            event.clone()
        };
        if prior.as_ref() == Some(&effective) {
            report.usage.existing += 1;
        } else {
            put(c, &effective)?;
            if prior.is_some() {
                report.usage.enriched += 1
            } else {
                report.usage.added += 1
            }
        }
        if is_agy {
            let mut insert = c.prepare_cached("insert or ignore into anchors values(?,?)")?;
            if bindings.is_empty() {
                insert.execute(params![original, event.uid])?;
            }
            for (anchor, req) in bindings {
                insert.execute(params![req, event.uid])?;
                insert.execute(params![format!("{anchor}:{req}"), event.uid])?;
            }
        } else if event.entry.ends_with("-api") || event.entry == "ai-studio" {
            // Independent source evidence must survive undo of a later (or earlier) API batch.
            let previous: Option<String> = c
                .query_row(
                    "select payload from original_api where uid=?",
                    [&event.uid],
                    |r| r.get(0),
                )
                .optional()?;
            let base = match previous.as_ref() {
                Some(s) => merge(&serde_json::from_str(s)?, &event, false)?,
                None => event.clone(),
            };
            let text = serde_json::to_string(&base)?;
            if previous.as_ref() != Some(&text) {
                c.execute(
                    "insert or replace into original_api values(?,?)",
                    params![event.uid, text],
                )?;
            }
        }
        let project: Option<String> = r.get(13)?;
        let account: Option<String> = r.get(14)?;
        let task: Option<String> = r.get(15)?;
        if project.is_some() || account.is_some() || task.is_some() {
            c.execute("insert into annotations values(?,?,?,?) on conflict(uid) do update set project=coalesce(project,excluded.project),account=coalesce(account,excluded.account),task=coalesce(task,excluded.task) where (project is null and excluded.project is not null) or (account is null and excluded.account is not null) or (task is null and excluded.task is not null)",params![event.uid,project,account,task])?;
        }
    }
    Ok(())
}

fn notes(src: &Connection, c: &Connection, report: &mut Report) -> Result<()> {
    let mut q = src.prepare("select id,kind,day,entry,data from notes")?;
    let mut rows = q.query([])?;
    while let Some(r) = rows.next()? {
        let id: String = r.get(0)?;
        let mut item: Value = serde_json::from_str(&r.get::<_, String>(4)?)?;
        ensure!(item.is_object() && !id.is_empty(), "原版手工账目无效");
        item["id"] = json!(id);
        for (i, k) in ["kind", "day", "entry"].iter().enumerate() {
            item[k] = json!(r.get::<_, String>(i + 1)?);
        }
        let note = prepare_note(&item)?;
        let old: Option<String> = c
            .query_row("select payload from notes where id=?", [&id], |r| r.get(0))
            .optional()?;
        match old {
            None => {
                c.execute(
                    "insert into notes values(?,?)",
                    params![id, note.to_string()],
                )?;
                report.notes.added += 1;
            }
            Some(s) if serde_json::from_str::<Value>(&s)? == note => report.notes.existing += 1,
            Some(_) => {
                report.notes.skipped += 1;
                report.detail(&id, "同一笔手工账目内容不同，保留 Rust 版");
            }
        }
    }
    Ok(())
}

fn prices(src: &Connection, c: &Connection, report: &mut Report) -> Result<()> {
    if !table(src, "codex_search_settings")? {
        return Ok(());
    }
    let raw: Option<String> = src
        .query_row(
            "select value from codex_search_settings where key='pricing_overrides'",
            [],
            |r| r.get(0),
        )
        .optional()?;
    let Some(raw) = raw else { return Ok(()) };
    let incoming: Value = serde_json::from_str(&raw)?;
    let mut rates = setting(c, "prices", json!({}))?;
    for (model, value) in incoming.as_object().context("自定义单价格式无效")? {
        ensure!(
            !model.trim().is_empty()
                && model.chars().count() <= 160
                && !model.chars().any(char::is_control),
            "自定义型号无效"
        );
        let mut rate = json!({});
        for k in ["input", "cache_read", "cache_write", "output"] {
            let n = value[k]
                .as_f64()
                .filter(|n| (0.0..=10000.0).contains(n))
                .context("原版单价无效")?;
            rate[k] = json!(n);
        }
        match rates.get(model) {
            Some(current) if current == &rate => report.prices.existing += 1,
            Some(_) => {
                report.prices.skipped += 1;
                report.detail(model, "已有不同单价，保留 Rust 版");
            }
            None => {
                rates[model] = rate;
                report.prices.added += 1;
            }
        }
    }
    if report.prices.added > 0 {
        save(c, "prices", &rates)?;
    }
    Ok(())
}

pub fn import(dst: &mut Connection, raw: &str) -> Result<Value> {
    let path = Path::new(raw.trim().trim_matches('"'));
    ensure!(
        is_local(path),
        "请选择本机磁盘上的原版 ledger.sqlite3 或完整备份"
    );
    let path = path.canonicalize().context("找不到所选文件，请检查路径")?;
    ensure!(is_local(&path) && path.is_file(), "请选择本机账本文件");
    // Deny writers and replacement throughout the read. Never create a WAL/SHM next to the source.
    let _guard = fs::OpenOptions::new()
        .read(true)
        .share_mode(1)
        .open(&path)
        .context("无法独占只读快照，请先在原版「数据接入」停止本地服务，或选择已关闭的完整备份")?;
    for suffix in ["-wal", "-journal"] {
        let mut side = path.as_os_str().to_owned();
        side.push(suffix);
        match fs::metadata(side) {
            Ok(m) => ensure!(m.len() == 0, "原版仍有未合并日志，请正常停止原版服务后重试"),
            Err(e) if e.kind() == ErrorKind::NotFound => (),
            Err(e) => return Err(e.into()),
        }
    }
    let src = crate::discovery::readonly(&path)?;
    src.execute_batch("pragma trusted_schema=off; pragma query_only=on; begin;")?;
    let version: i64 = src.query_row("pragma user_version", [], |r| r.get(0))?;
    ensure!(
        src.query_row("pragma application_id", [], |r| r.get::<_, i64>(0))? == 0
            && (0..=4).contains(&version)
            && table(&src, "project_roots")?
            && table(&src, "events")?
            && table(&src, "notes")?
            && table(&src, "scans")?,
        "请选择受支持的原版账本或其完整备份"
    );
    let tx = dst.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let before = tx.total_changes();
    let mut report = Report::default();
    usage(&src, &tx, version == 4, &mut report)?;
    notes(&src, &tx, &mut report)?;
    prices(&src, &tx, &mut report)?;
    if tx.total_changes() != before {
        bump(&tx)?;
    }
    tx.commit()?;
    Ok(serde_json::to_value(report)?)
}
