use crate::db::*;
use anyhow::{ensure, Context, Result};
use rusqlite::{params, types::ValueRef, Connection, Params};
use serde_json::{json, Value};
pub fn rows(c: &Connection, sql: &str, p: impl Params) -> Result<Vec<Value>> {
    let mut st = c.prepare(sql)?;
    let names = st
        .column_names()
        .iter()
        .map(|s| s.to_string())
        .collect::<Vec<_>>();
    let result = st
        .query_map(p, |r| {
            let mut obj = json!({});
            for (i, k) in names.iter().enumerate() {
                obj[k] = match r.get_ref(i)? {
                    ValueRef::Null => Value::Null,
                    ValueRef::Integer(n) => json!(n),
                    ValueRef::Real(n) => json!(n),
                    ValueRef::Text(s) => json!(String::from_utf8_lossy(s)),
                    ValueRef::Blob(_) => Value::Null,
                };
            }
            Ok(obj)
        })?
        .collect::<rusqlite::Result<_>>()?;
    Ok(result)
}
pub fn options(c: &Connection) -> Result<Value> {
    let mut values = json!({});
    for k in crate::query::DIMS.iter().filter(|x| **x != "uid") {
        values[k] = json!(rows(
            c,
            &format!("select distinct {k} v from effective order by {k}"),
            []
        )?
        .iter()
        .map(|r| &r["v"])
        .collect::<Vec<_>>())
    }
    let mut entries = json!({});
    for (k, m) in CATALOG["entries"].as_object().unwrap() {
        entries[k] = m[0].clone()
    }
    Ok(json!({"values":values,"entries":entries,"subscriptions":{},"codex_profile":null}))
}
pub fn sources(c: &Connection) -> Result<Value> {
    let mut list = vec![];
    for (k, m) in CATALOG["entries"].as_object().unwrap() {
        let mut v = setting(c, k, json!({}))?;
        let (n, lo, hi): (i64, Option<i64>, Option<i64>) = c.query_row(
            "select count(*),min(ts),max(ts) from events where entry=?",
            [k],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )?;
        let observed=rows(c,"select count(distinct uid) n,min(json_extract(payload,'$.ts')) lo,max(json_extract(payload,'$.ts')) hi from origins where json_extract(payload,'$.entry')=?", [k])?.pop().unwrap();
        let lo = lo.into_iter().chain(observed["lo"].as_i64()).min();
        let hi = hi.into_iter().chain(observed["hi"].as_i64()).max();
        v["observed"] = observed["n"].clone();
        v["key"] = json!(k);
        v["name"] = m[0].clone();
        v["platform"] = m[1].clone();
        v["mode"] = m[2].clone();
        v["method"] = m[3].clone();
        v["n"] = json!((n > 0).then_some(n));
        v["historical"] = json!(lo.is_some());
        v["first"] = json!(lo.map(iso));
        v["last"] = json!(hi.map(iso));
        list.push(v)
    }
    Ok(json!(list))
}
fn clean(s: &str, n: usize) -> Result<&str> {
    ensure!(
        s.chars().count() <= n && !s.chars().any(char::is_control),
        "文本字段无效"
    );
    Ok(s.trim())
}
pub fn correct(c: &mut Connection, b: &Value) -> Result<Value> {
    let target = string(b, "target");
    let action = string(b, "action");
    let val = clean(b["value"].as_str().unwrap_or(""), 160)?;
    let reason = clean(b["reason"].as_str().unwrap_or(""), 300)?;
    ensure!(!reason.is_empty(), "须填写依据");
    let tx = c.transaction()?;
    let mut key = target;
    let previous = match action.as_str() {
        "alias" => {
            if let Some(path) = b["path"].as_str().filter(|s| !s.is_empty()) {
                key = project(&tx, path)?
            }
            ensure!(!key.is_empty() && key != "未归类", "须选定项目");
            let prev = rows(&tx, "select logical v from aliases where raw=?", [&key])?;
            tx.execute("delete from aliases where raw=?", [&key])?;
            if !val.is_empty() {
                tx.execute("insert into aliases values(?,?)", params![key, val])?;
            }
            prev
        }
        "project" | "task" | "account" => {
            ensure!(get(&tx, &key)?.is_some(), "记录不存在");
            let prev = rows(
                &tx,
                &format!("select {action} v from annotations where uid=?"),
                [&key],
            )?;
            tx.execute("insert or ignore into annotations(uid) values(?)", [&key])?;
            tx.execute(
                &format!("update annotations set {action}=? where uid=?"),
                params![(!val.is_empty()).then_some(val), key],
            )?;
            prev
        }
        "resolve" => {
            let id = val.parse::<i64>()?;
            let data: String = tx.query_row(
                "select payload from origins where rowid=? and uid=?",
                params![id, key],
                |r| r.get(0),
            )?;
            let mut e: Event = serde_json::from_str(&data)?;
            if get(&tx, &key)?.is_some_and(|e| e.entry == "agy-shared") {
                e.entry = "agy-shared".into()
            }
            put(&tx, &e)?;
            tx.execute("delete from conflicts where uid=?", [&key])?;
            vec![]
        }
        _ => anyhow::bail!("未知更正方式"),
    };
    tx.execute(
        "insert into changes values(?,?,?,?,?,?)",
        params![
            now(),
            key,
            action,
            previous.first().and_then(|v| v["v"].as_str()),
            val,
            reason
        ],
    )?;
    bump(&tx)?;
    tx.commit()?;
    Ok(json!({"ok":true}))
}
pub fn review(c: &Connection, path: &str, q: &Q) -> Result<Value> {
    let page = crate::query::arg(q, "page", "0").parse::<u32>()?;
    let off = i64::from(page) * 40;
    let changes = "select stamp as at,target,action,previous,value,reason from changes";
    match path {
        "/api/manage" => {
            let mut aliases = json!({});
            for r in rows(c, "select * from aliases", [])? {
                aliases[r["raw"].as_str().unwrap()] = r["logical"].clone()
            }
            Ok(
                json!({"page":page,"limit":40,"batch_count":c.query_row("select count(*) from batches",[],|r|r.get::<_,i64>(0))?,"batches":rows(c,"select * from batches order by id desc limit 40 offset ?",[off])?,"disputes":rows(c,"select conflicts.*,model from conflicts join events using(uid)",[])?,"aliases":aliases,"projects":rows(c,"select distinct project from events order by project",[])?.iter().map(|r|&r["project"]).collect::<Vec<_>>(),"changes":rows(c,&format!("{changes} order by rowid desc limit 40"),[])?}),
            )
        }
        "/api/batch" => {
            let id = q.get("id").context("缺少批次")?.parse::<i64>()?;
            let mut lines=rows(c,"select line,uid,outcome,payload,total effective_total,events.uid is not null present from contributions left join events using(uid) where batch=? order by line limit 41 offset ?",params![id,off])?;
            let more = lines.len() > 40;
            lines.truncate(40);
            for l in &mut lines {
                l["record"] = serde_json::from_str(l["payload"].as_str().unwrap())?;
                l.as_object_mut().unwrap().remove("payload");
            }
            let mut batch = rows(c, "select * from batches where id=?", [id])?
                .pop()
                .context("批次不存在")?;
            batch["fingerprint"] = setting(c, &format!("batch-sha:{id}"), Value::Null)?;
            Ok(json!({"rows":lines,"batch":batch,"more":more}))
        }
        _ => {
            let id = q.get("uid").context("缺少事件 ID")?;
            let mut obs = rows(
                c,
                "select rowid id,origin,payload from origins where uid=? order by rowid desc",
                [id],
            )?;
            for o in &mut obs {
                let e: Value = serde_json::from_str(o["payload"].as_str().unwrap())?;
                o["entry"] = e["entry"].clone();
                o["source"] = e["source"].clone();
                o["record"] = e;
                o.as_object_mut().unwrap().remove("payload");
            }
            let mut batches=rows(c,"select batch id,line,undone,payload from contributions join batches on batch=batches.id where uid=?",[id])?;
            for b in &mut batches {
                b["record"] = serde_json::from_str(b["payload"].as_str().unwrap())?;
                b.as_object_mut().unwrap().remove("payload");
            }
            Ok(
                json!({"current":rows(c,"select * from effective where uid=?",[id])?.first(),"observations":obs,"batches":batches,"changes":rows(c,&format!("{changes} where target=? order by rowid desc"),[id])?,"disputed":c.query_row("select exists(select 1 from conflicts where uid=?)",[id],|r|r.get::<_,bool>(0))?}),
            )
        }
    }
}
pub fn notes(c: &Connection, q: &Q) -> Result<Value> {
    let mut list = vec![];
    for r in rows(
        c,
        "select payload from notes order by json_extract(payload,'$.day') desc,rowid desc",
        [],
    )? {
        let mut v: Value = serde_json::from_str(r["payload"].as_str().unwrap())?;
        let proj = v["project"]
            .as_str()
            .filter(|s| !s.is_empty())
            .unwrap_or("未归类");
        v["logical_project"] = rows(c, "select logical from aliases where raw=?", [proj])?
            .first()
            .map(|r| r["logical"].clone())
            .unwrap_or(json!(proj));
        if q.iter().all(|(k, s)| {
            s.is_empty()
                || match k.as_str() {
                    "from" => v["day"].as_str() >= Some(s),
                    "to" => v["day"].as_str() <= Some(s),
                    "project" => v["logical_project"] == s.as_str(),
                    "account" | "plan" | "cycle_start" | "cycle_end" => {
                        v[k].as_str()
                            .filter(|s| !s.is_empty())
                            .unwrap_or(if k == "account" {
                                "账号未确认"
                            } else {
                                ""
                            })
                            == s
                    }
                    _ => true,
                }
        }) {
            list.push(v)
        }
    }
    Ok(json!(list))
}
pub fn save_note(c: &Connection, b: &Value) -> Result<Value> {
    let mut v = json!({});
    for k in [
        "kind",
        "entry",
        "day",
        "title",
        "project",
        "account",
        "plan",
        "cycle_start",
        "cycle_end",
        "media",
        "currency",
        "unit",
    ] {
        v[k] = json!(clean(b[k].as_str().unwrap_or(""), 160)?);
    }
    let _: chrono::NaiveDate = string(&v, "day").parse()?;
    ensure!(
        (CATALOG["entries"].get(string(&v, "entry")).is_some()
            || (["subscription", "quota"].contains(&v["kind"].as_str().unwrap_or(""))
                && CATALOG["services"].get(string(&v, "entry")).is_some()))
            && v["entry"] != "prism",
        "未知入口"
    );
    let kind = string(&v, "kind");
    ensure!(
        ["media", "subscription", "api_fee", "quota"].contains(&kind.as_str()),
        "未知账目类型"
    );
    for k in ["cycle_start", "cycle_end"] {
        if v[k] != "" {
            let _: chrono::NaiveDate = string(&v, k).parse()?;
        }
    }
    ensure!(
        (v["cycle_start"] == "") == (v["cycle_end"] == "")
            && string(&v, "cycle_start") <= string(&v, "cycle_end"),
        "周期无效"
    );
    if kind == "media" {
        for k in ["produced", "usable", "retries", "minutes"] {
            if b[k].is_null() && ["minutes", "retries"].contains(&k) {
                continue;
            }
            ensure!(
                b[k].as_i64()
                    .is_some_and(|n| (0..=100_000_000).contains(&n)),
                "数量或时间无效"
            );
            v[k] = b[k].clone()
        }
        ensure!(
            v["usable"].as_i64() <= v["produced"].as_i64(),
            "可用数超出产出"
        )
    }
    if kind != "media" || !b["amount"].is_null() && b["amount"] != "" {
        let amount = b["amount"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| b["amount"].to_string());
        ensure!(
            amount
                .parse::<f64>()
                .is_ok_and(|n| n.is_finite() && n >= 0.),
            "金额无效"
        );
        if kind != "quota" {
            ensure!(
                string(&v, "currency").len() == 3
                    && string(&v, "currency")
                        .bytes()
                        .all(|b| b.is_ascii_uppercase()),
                "币种无效"
            )
        }
        v["amount"] = json!(amount)
    }
    let id = b["id"]
        .as_str()
        .filter(|s| !s.is_empty())
        .map(str::to_owned)
        .unwrap_or_else(|| identity((now(), &v)));
    v["id"] = json!(id);
    c.execute(
        "insert or replace into notes values(?,?)",
        params![id, v.to_string()],
    )?;
    Ok(json!({"id":id}))
}
