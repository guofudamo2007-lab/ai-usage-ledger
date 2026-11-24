use crate::{collect, db::*, imports, manage, query, quota};
use anyhow::{ensure, Context, Result};
use chrono::Utc;
use rusqlite::Connection;
use serde_json::{json, Value};
use std::{
    fs,
    io::Read,
    os::windows::process::CommandExt,
    path::PathBuf,
    process::Command,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
};
use tiny_http::{Header, Method, Request, Response, StatusCode};

include!(concat!(env!("OUT_DIR"), "/assets.rs"));
pub struct App {
    pub db: PathBuf,
    pub codex: PathBuf,
    pub gemini: PathBuf,
    pub port: u16,
    pub id: String,
    pub scan: Mutex<Value>,
    pub stop: AtomicBool,
    pub quota_running: Mutex<bool>,
    pub widget_cache: Mutex<Option<(String, Value)>>,
}
impl App {
    pub fn sync(self: &Arc<Self>) {
        let mut state = self.scan.lock().unwrap();
        if state["running"] == true {
            return;
        }
        *state = json!({"running":true,"files":0,"done":0});
        drop(state);
        let app = self.clone();
        thread::spawn(move || {
            let res = open(&app.db).and_then(|mut db| {
                collect::scan(&mut db, &app.codex, &app.gemini, |p| {
                    *app.scan.lock().unwrap() = p
                })
            });
            *app.scan.lock().unwrap() = json!({"running":false,"error":res.err().map(|_|"本机采集未完成，请查看来源状态后重试"),"finished":now()});
        });
    }
    fn quota_key(&self, k: &str) -> String {
        format!(
            "quota:{k}:{}",
            identity(if k == "codex" {
                &self.codex
            } else {
                &self.gemini
            })
        )
    }
    fn refresh_quota(&self, c: &Connection, k: &str, auto: bool) -> Result<Value> {
        let key = self.quota_key(k);
        let prior = setting(c, &key, Value::Null)?;
        if auto
            && prior["at"]
                .as_str()
                .and_then(|t| chrono::DateTime::parse_from_rfc3339(t).ok())
                .is_some_and(|d| (0..300).contains(&(Utc::now() - d.to_utc()).num_seconds()))
        {
            return Ok(prior);
        }
        let result = quota::live(k, &self.codex, self.db.parent().unwrap())?;
        save(c, &key, &result)?;
        Ok(result)
    }
    pub fn widget(&self, tz: &str) -> Result<()> {
        ensure!(
            tz.is_empty() || tz.parse::<chrono_tz::Tz>().is_ok(),
            "未知时区"
        );
        const EXE: &[u8] = include_bytes!(concat!(env!("OUT_DIR"), "/MeterWidget.exe"));
        let dir = self
            .db
            .parent()
            .unwrap()
            .join("components")
            .join(&hash(EXE)[..16]);
        fs::create_dir_all(&dir)?;
        let exe = dir.join("MeterWidget.exe");
        if fs::read(&exe).ok().as_deref() != Some(EXE) {
            let tmp = dir.join(format!(
                "{}.tmp",
                identity((now(), format!("{:?}", thread::current().id())))
            ));
            fs::write(&tmp, EXE)?;
            if let Err(e) = fs::rename(&tmp, &exe) {
                let _ = fs::remove_file(tmp);
                ensure!(fs::read(&exe)?.as_slice() == EXE, "小组件解包失败：{e}")
            }
        }
        Command::new(exe)
            .args([self.port.to_string(), tz.to_owned()])
            .arg(self.db.parent().unwrap().join("widget-window.json"))
            .creation_flags(0x08000000)
            .spawn()?;
        Ok(())
    }
    fn route(
        self: &Arc<Self>,
        c: &mut Connection,
        path: &str,
        q: &Q,
        write: bool,
        body: &[u8],
    ) -> Result<Value> {
        let data = || -> Result<Value> { Ok(serde_json::from_slice(body)?) };
        match (path, write) {
            ("/api/instance", false) => {
                Ok(json!({"app":"kedu-native","version":env!("CARGO_PKG_VERSION"),"instance":self.id}))
            }
            ("/api/status", false) => {
                let mut v = self.scan.lock().unwrap().clone();
                v["revision"] = setting(c, "revision", json!(0))?;
                let mut entries = vec![];
                for k in ["codex", "agy", "agy-ide", "agy-cli"] {
                    let s = setting(c, k, Value::Null)?;
                    if s["skipped"].as_i64().unwrap_or(0) > 0
                        || s["failures"].as_i64().unwrap_or(0) > 0
                        || s["health"] == "来源失联"
                    {
                        entries.push(k)
                    }
                }
                v["alerts"] = json!({"entries":entries,"conflicts":c.query_row("select count(*) from conflicts",[],|r|r.get::<_,i64>(0))?});
                Ok(v)
            }
            ("/api/sync", true) => {
                self.sync();
                Ok(self.scan.lock().unwrap().clone())
            }
            ("/api/stop", true) => {
                self.stop.store(true, Ordering::Release);
                Ok(json!({"ok":true}))
            }
            ("/api/view", false) => query::view(c, q, false),
            ("/api/options", false) => {
                let mut v = manage::options(c)?;
                v["original_import"] = json!(true);
                v["subscriptions"] = crate::subscriptions::catalog(c, self.db.parent().unwrap())?;
                Ok(v)
            }
            ("/api/sources", false) => manage::sources(c),
            ("/api/appearance", _) => {
                let mut value = setting(c, "appearance", CATALOG["appearance"].clone())?;
                if write {
                    let b = data()?;
                    let preset = b["preset"].as_str().context("未知主题")?;
                    ensure!(
                        CATALOG["presets"].get(preset).is_some()
                            && ["light", "dark", "system"]
                                .contains(&b["mode"].as_str().unwrap_or(""))
                            && CATALOG["fonts"].as_array().unwrap().contains(&b["font"]),
                        "外观无效"
                    );
                    for k in ["dark", "light"] {
                        ensure!(
                            b[k].as_array().is_some_and(|a| a.len() == 3
                                && a.iter().all(|v| v.as_str().is_some_and(|s| s.len() == 7
                                    && s.starts_with('#')
                                    && s.as_bytes()[1..].iter().all(u8::is_ascii_hexdigit)))),
                            "颜色无效"
                        );
                        value[k] = b[k].clone();
                    }
                    for k in ["mode", "preset", "font"] {
                        value[k] = b[k].clone()
                    }
                    save(c, "appearance", &value)?
                }
                Ok(json!({"settings":value,"presets":CATALOG["presets"],"fonts":CATALOG["fonts"]}))
            }
            ("/api/pricing", _) => {
                let tx = c.transaction_with_behavior(if write {
                    rusqlite::TransactionBehavior::Immediate
                } else {
                    rusqlite::TransactionBehavior::Deferred
                })?;
                let mut prices = setting(&tx, "prices", json!({}))?;
                if write {
                    let b = data()?;
                    let m = b["model"].as_str().context("缺少型号")?.trim();
                    ensure!(
                        !m.is_empty()
                            && m.chars().count() <= 160
                            && !m.chars().any(char::is_control),
                        "型号无效"
                    );
                    if b["reset"] == true {
                        prices.as_object_mut().unwrap().remove(m);
                    } else {
                        let mut rate = json!({});
                        for k in ["input", "output", "cache_write", "cache_read"] {
                            let n = b[k].as_f64().context("单价无效")?;
                            ensure!((0.0..=10000.).contains(&n), "单价超出范围");
                            rate[k] = json!(n)
                        }
                        prices[m] = rate
                    }
                    save(&tx, "prices", &prices)?;
                    bump(&tx)?;
                }
                tx.commit()?;
                Ok(json!({"overrides":prices,"version":CATALOG["version"]}))
            }
            ("/api/widget/open", true) => {
                self.widget(query::arg(q, "tz", ""))?;
                Ok(json!({"ok":true}))
            }
            ("/api/widget/refresh", true) => {
                if query::arg(q, "period", "today") == "quota" {
                    let mut running = self.quota_running.lock().unwrap();
                    if !*running {
                        *running = true;
                        let app = self.clone();
                        thread::spawn(move || {
                            if let Ok(c) = open(&app.db) {
                                for k in ["codex", "agy"] {
                                    let _ = app.refresh_quota(&c, k, false);
                                }
                                let dir = app.db.parent().unwrap();
                                for k in crate::subscriptions::READERS {
                                    if crate::subscriptions::available(k, dir) {
                                        let _ = crate::subscriptions::snapshot(
                                            &c, dir, k, true, true, None,
                                        );
                                    }
                                }
                            }
                            *app.quota_running.lock().unwrap() = false;
                        });
                    }
                } else {
                    self.sync()
                }
                Ok(json!({"ok":true}))
            }
            ("/api/widget", false) => {
                let p = query::arg(q, "period", "today");
                let mut v =
                    json!({"appearance":setting(c,"appearance",CATALOG["appearance"].clone())?});
                if p == "quota" {
                    let mut quotas = vec![];
                    for (k, name) in [("codex", "Codex"), ("agy", "Antigravity")] {
                        let mut s = setting(c, &self.quota_key(k), Value::Null)?;
                        if s.is_object() {
                            s["name"] = json!(name);
                            quotas.push(s)
                        }
                    }
                    for k in crate::subscriptions::READERS {
                        let mut s = setting(c, &format!("subscription:{k}"), Value::Null)?;
                        if s.is_object() {
                            s["name"] = CATALOG["services"][k][0].clone();
                            quotas.push(s);
                        }
                    }
                    v["quotas"] = json!(quotas);
                    v["syncing"] = json!(*self.quota_running.lock().unwrap());
                } else {
                    ensure!(["today", "week"].contains(&p), "未知小组件范围");
                    let tz = query::arg(q, "tz", "UTC");
                    let zone = tz.parse::<chrono_tz::Tz>()?;
                    let key = format!(
                        "{p}:{tz}:{}:{}",
                        Utc::now().with_timezone(&zone).date_naive(),
                        setting(c, "revision", json!(0))?
                    );
                    let mut cache = self.widget_cache.lock().unwrap();
                    if !cache.as_ref().is_some_and(|(k, _)| k == &key) {
                        let args = Q::from([("period".into(), p.into()), ("tz".into(), tz.into())]);
                        let data = query::view(c, &args, false)?;
                        *cache = Some((key, data))
                    }
                    let data = &cache.as_ref().unwrap().1;
                    for k in ["scope", "revision", "totals"] {
                        v[k] = data[k].clone()
                    }
                    v["top"] = data["rank"]
                        .as_array()
                        .and_then(|a| a.iter().find(|r| !r["total"].is_null()))
                        .map(|r| r["model"].clone())
                        .unwrap_or_default();
                    for k in ["value", "value_n"] {
                        v[k] = v["totals"][k].clone()
                    }
                    v["syncing"] = self.scan.lock().unwrap()["running"].clone();
                }
                Ok(v)
            }
            ("/api/import", true) => imports::import(
                c,
                body,
                query::arg(q, "format", "json"),
                query::arg(q, "preview", "") == "1",
            ),
            ("/api/undo", true) => {
                let b = data()?;
                imports::undo(c, b["batch"].as_i64().context("缺少批次")?)
            }
            ("/api/original/pick", true) => crate::legacy::pick(),
            ("/api/original/import", true) => {
                let b = data()?;
                crate::legacy::import(c, b["path"].as_str().context("请选择原版账本文件")?)
            }
            ("/api/notes", false) => manage::notes(c, q),
            ("/api/notes", true) => manage::save_note(c, &data()?),
            ("/api/notes/remove", true) => {
                c.execute("delete from notes where id=?", [string(&data()?, "id")])?;
                Ok(json!({"ok":true}))
            }
            ("/api/manage" | "/api/batch" | "/api/evidence", false) => manage::review(c, path, q),
            ("/api/correct", true) => manage::correct(c, &data()?),
            ("/api/codex-quota" | "/api/agy-quota", _) => {
                let k = if path.contains("codex") {
                    "codex"
                } else {
                    "agy"
                };
                if write {
                    self.refresh_quota(c, k, query::arg(q, "auto", "") == "1")
                } else {
                    setting(c, &self.quota_key(k), Value::Null)
                }
            }
            (path, _) if path.starts_with("/api/subscription/") => {
                let import = query::arg(q, "import", "") == "1" && write;
                let k = path.trim_start_matches("/api/subscription/");
                if import {
                    ensure!(
                        matches!(k, "claude-code" | "kimi-code") && body.len() <= 1024 * 1024,
                        "不支持该快照"
                    )
                }
                let value = if import { Some(data()?) } else { None };
                crate::subscriptions::snapshot(
                    c,
                    self.db.parent().unwrap(),
                    k,
                    write,
                    query::arg(q, "force", "") == "1",
                    value.as_ref(),
                )
            }
            ("/api/codex-search", _) => {
                if write {
                    crate::discovery::set_backup(
                        c,
                        data()?["backup"].as_str().context("目录无效")?,
                    )?
                }
                crate::discovery::status(c, &self.codex)
            }
            _ => anyhow::bail!("此接口尚未在 Rust 独立版提供"),
        }
    }
}
fn header(k: &str, v: &str) -> Header {
    Header::from_bytes(k.as_bytes(), v.as_bytes()).unwrap()
}
fn send(req: Request, status: u16, mime: &str, body: Vec<u8>, download: Option<&str>) {
    let mut r=Response::from_data(body).with_status_code(StatusCode(status)).with_header(header("Content-Type",mime)).with_header(header("Cache-Control","no-store")).with_header(header("X-Content-Type-Options","nosniff")).with_header(header("Content-Security-Policy","default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"));
    if let Some(fmt) = download {
        r.add_header(header(
            "Content-Disposition",
            &format!("attachment; filename=\"kedu-rust-usage.{fmt}\""),
        ))
    }
    let _ = req.respond(r);
}
pub fn handle(app: &Arc<App>, mut req: Request) {
    let host = format!("127.0.0.1:{}", app.port);
    let h = |n: &'static str| {
        req.headers()
            .iter()
            .find(|h| h.field.equiv(n))
            .map(|h| h.value.as_str())
    };
    let write = *req.method() == Method::Post;
    if h("Host") != Some(&host)
        || (write
            && (h("X-Ledger-Local") != Some("1")
                || h("Origin").is_some_and(|s| s != format!("http://{host}"))))
    {
        return send(
            req,
            403,
            "application/json",
            br#"{"error":"localhost only"}"#.to_vec(),
            None,
        );
    }
    if !write && *req.method() != Method::Get {
        return send(req, 405, "text/plain", vec![], None);
    }
    let url = req.url().to_string();
    let (path, params) = url.split_once('?').unwrap_or((&url, ""));
    let q: Q = form_urlencoded::parse(params.as_bytes())
        .into_owned()
        .collect();
    if !write {
        if let Some((mime, b)) = asset(path) {
            let mut data = b.to_vec();
            if path == "/" {
                data = String::from_utf8_lossy(b)
                    .replace(
                        "<title>刻度 · AI 用量账本",
                        "<title>刻度 Rust · AI 用量账本",
                    )
                    .replace(
                        "<small>AI 用量账本</small>",
                        "<small>AI 用量账本 · Rust</small>",
                    )
                    .into_bytes()
            }
            return send(req, 200, mime, data, None);
        }
    }
    let res = (|| -> Result<(Vec<u8>, &str, Option<&str>)> {
        let mut db = open(&app.db)?;
        if path == "/api/export" && !write {
            let records = query::view(&db, &q, true)?;
            let fmt = query::arg(&q, "format", "csv");
            if fmt == "json" {
                return Ok((
                    serde_json::to_vec(
                        &json!({"schema":"meter.export.v1","timezone":query::arg(&q,"tz","UTC"),"filters":q,"records":records}),
                    )?,
                    "application/json",
                    Some("json"),
                ));
            }
            ensure!(fmt == "csv", "未知导出格式");
            let mut w = csv::Writer::from_writer(vec![0xef, 0xbb, 0xbf]);
            let keys = [
                "timestamp",
                "local_time",
                "timezone",
                "platform",
                "entry",
                "entry_name",
                "vendor",
                "model",
                "project",
                "total",
                "input",
                "output",
                "cache_read",
                "cache_write",
                "reasoning",
                "net",
                "source",
                "uid",
                "account",
                "task",
                "raw_project",
            ];
            w.write_record(keys)?;
            for r in records.as_array().unwrap() {
                w.write_record(keys.iter().map(|k| {
                    if *k == "timezone" {
                        return query::arg(&q, "tz", "UTC").to_owned();
                    }
                    match &r[k] {
                        Value::Null => String::new(),
                        Value::String(s) if s.starts_with(['=', '+', '-', '@', '\t', '\r']) => {
                            format!("'{s}")
                        }
                        Value::String(s) => s.clone(),
                        v => v.to_string(),
                    }
                }))?
            }
            return Ok((w.into_inner()?, "text/csv; charset=utf-8", Some("csv")));
        }
        let mut body = vec![];
        if write {
            let max = 32 * 1024 * 1024;
            ensure!(req.body_length().unwrap_or(0) <= max, "导入上限为 32 MiB");
            req.as_reader()
                .take(max as u64 + 1)
                .read_to_end(&mut body)?;
            ensure!(body.len() <= max, "请求过大")
        }
        Ok((
            serde_json::to_vec(&app.route(&mut db, path, &q, write, &body)?)?,
            "application/json",
            None,
        ))
    })();
    match res {
        Ok((body, mime, name)) => send(req, 200, mime, body, name),
        Err(e) => send(
            req,
            400,
            "application/json",
            json!({"error":e.to_string()}).to_string().into_bytes(),
            None,
        ),
    }
}
