use crate::{agy, db::*};
use anyhow::{ensure, Context, Result};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::{
    collections::BTreeSet,
    fs,
    io::{BufRead, BufReader, Read, Seek, SeekFrom},
    path::{Path, PathBuf},
};
use walkdir::WalkDir;
#[derive(serde::Serialize, serde::Deserialize)]
struct Cursor {
    session: String,
    turn: String,
    model: String,
    provider: String,
    folder: String,
    born: Option<i64>,
    last: Option<i64>,
    offset: u64,
    proof: String,
    skipped: usize,
}
fn proof(file: &mut fs::File, end: u64) -> Result<String> {
    let mut sample = vec![0; end.min(512) as usize];
    file.seek(SeekFrom::Start(0))?;
    file.read_exact(&mut sample)?;
    let tail = end.min(256) as usize;
    let head = sample.len();
    sample.resize(head + tail, 0);
    file.seek(SeekFrom::Start(end - tail as u64))?;
    file.read_exact(&mut sample[head..])?;
    Ok(hash(sample))
}
pub fn codex(db: &Connection, path: &Path, key: &str) -> Result<(usize, usize)> {
    let mut file = fs::File::open(path)?;
    let compressed = path.extension().is_some_and(|s| s == "zst");
    let mut session = path
        .file_name()
        .unwrap_or_default()
        .to_string_lossy()
        .split(".jsonl")
        .next()
        .unwrap_or("")
        .to_owned();
    let (mut turn, mut model, mut provider, mut folder) = (
        String::new(),
        String::new(),
        String::new(),
        String::from("未归类"),
    );
    let mut born = None;
    let mut last = None;
    let (mut added, mut skipped, mut offset) = (0, 0, 0u64);
    if !compressed {
        let saved = db
            .query_row("select payload from cursors where id=?", [key], |r| {
                r.get::<_, String>(0)
            })
            .optional()?;
        if let Some(c) = saved.and_then(|s| serde_json::from_str::<Cursor>(&s).ok()) {
            if c.offset > 0
                && file.metadata()?.len() > c.offset
                && proof(&mut file, c.offset)? == c.proof
            {
                session = c.session;
                turn = c.turn;
                model = c.model;
                provider = c.provider;
                folder = c.folder;
                born = c.born;
                last = c.last;
                offset = c.offset;
                skipped = c.skipped;
            }
        }
        file.seek(SeekFrom::Start(offset))?;
    }
    let stream: Box<dyn Read> = if compressed {
        Box::new(zstd::Decoder::new(file)?)
    } else {
        Box::new(file)
    };
    let mut buf = BufReader::new(stream);
    let mut line = Vec::new();
    loop {
        line.clear();
        let len = (&mut buf)
            .take(16 * 1024 * 1024 + 1)
            .read_until(b'\n', &mut line)?;
        if len == 0 {
            break;
        }
        if line.last() != Some(&b'\n') {
            if len > 16 * 1024 * 1024 {
                if line.windows(13).any(|w| w == b"\"token_count\"") {
                    skipped += 1
                }
                let discarded = buf.skip_until(b'\n')?;
                if discarded > 0 {
                    offset += len as u64 + discarded as u64;
                }
                continue;
            }
            break;
        }
        offset += len as u64;
        if ![
            b"\"session_meta\"".as_slice(),
            b"\"turn_context\"",
            b"\"token_count\"",
            b"\"last_token_usage\"",
        ]
        .iter()
        .any(|key| line.windows(key.len()).any(|w| w == *key))
        {
            continue;
        }
        let res = (|| -> Result<()> {
            let o: Value = serde_json::from_slice(&line)?;
            let p = &o["payload"];
            match o["type"].as_str() {
                Some("session_meta") => {
                    session = p["id"]
                        .as_str()
                        .or(p["session_id"].as_str())
                        .unwrap_or(&session)
                        .to_owned();
                    provider = string(p, "model_provider");
                    folder = project(db, &string(p, "cwd"))?;
                    born = Some(stamp(
                        p["timestamp"]
                            .as_str()
                            .or(o["timestamp"].as_str())
                            .context("缺少时间")?,
                    )?);
                }
                Some("turn_context") => {
                    turn = string(p, "turn_id");
                    model = string(p, "model");
                    if let Some(s) = p["cwd"].as_str() {
                        folder = project(db, s)?
                    }
                }
                Some("event_msg") if p["type"] == "token_count" => {
                    if p["info"].is_null() && !p["rate_limits"].is_null() {
                        return Ok(());
                    }
                    let t = stamp(o["timestamp"].as_str().context("缺少时间")?)?;
                    ensure!(
                        !turn.is_empty() || born.is_none_or(|b| t >= b),
                        "分叉历史身份不明"
                    );
                    let info = &p["info"];
                    let cum = info["total_token_usage"]["total_tokens"].as_i64();
                    if cum.is_some() && last == cum {
                        return Ok(());
                    }
                    let u = &info["last_token_usage"];
                    let n = |k: &str| -> Result<Option<i64>> {
                        Ok(if u[k].is_null() {
                            None
                        } else {
                            Some(u[k].as_i64().context("用量非整数")?)
                        })
                    };
                    let usage = Usage {
                        total: n("total_tokens")?,
                        input: n("input_tokens")?,
                        output: n("output_tokens")?,
                        cache_read: n("cached_input_tokens")?,
                        cache_write: n("cache_write_input_tokens")?,
                        reasoning: n("reasoning_output_tokens")?,
                    }
                    .validate()?;
                    let uid = identity((
                        "codex-event",
                        if turn.is_empty() { &session } else { &turn },
                        t,
                        cum,
                        &usage,
                    ));
                    last = cum;
                    if get(db, &uid)?.is_none() {
                        let m = p["model"].as_str().filter(|s| !s.is_empty()).unwrap_or(
                            if model.is_empty() {
                                "Codex · 型号未记录"
                            } else {
                                &model
                            },
                        );
                        put(
                            db,
                            &Event {
                                uid,
                                ts: t,
                                entry: "codex".into(),
                                vendor: vendor(
                                    m,
                                    if provider == "openai" {
                                        "OpenAI"
                                    } else {
                                        "厂商未确认"
                                    },
                                ),
                                model: m.into(),
                                source: "Codex · token_count".into(),
                                project: folder.clone(),
                                usage,
                            },
                        )?;
                        added += 1
                    }
                }
                _ if p["type"] == "token_count" || p["info"].get("last_token_usage").is_some() => {
                    anyhow::bail!("未知用量事件")
                }
                _ => (),
            };
            Ok(())
        })();
        if res.is_err() {
            skipped += 1
        }
    }
    if !compressed {
        let proof = proof(&mut fs::File::open(path)?, offset)?;
        let cursor = Cursor {
            session,
            turn,
            model,
            provider,
            folder,
            born,
            last,
            offset,
            proof,
            skipped,
        };
        db.execute(
            "insert or replace into cursors values(?,?)",
            params![key, serde_json::to_string(&cursor)?],
        )?;
    }
    Ok((added, skipped))
}
fn signature(p: &Path) -> Result<String> {
    fn stat(p: &Path) -> Result<String> {
        let m = p.metadata()?;
        Ok(format!("{}:{:?}", m.len(), m.modified()?))
    }
    let mut s = stat(p)?;
    if p.extension().is_some_and(|e| e == "db") {
        let wal = PathBuf::from(format!("{}-wal", p.display()));
        if wal.exists() {
            s.push_str(&stat(&wal)?);
        }
    }
    Ok(s)
}
pub fn scan(
    c: &mut Connection,
    codex_home: &Path,
    gemini_home: &Path,
    progress: impl Fn(Value),
) -> Result<()> {
    for (entry, dirs) in [
        (
            "codex",
            vec![
                codex_home.join("sessions"),
                codex_home.join("archived_sessions"),
            ],
        ),
        ("agy", vec![gemini_home.join("antigravity/conversations")]),
        (
            "agy-cli",
            vec![gemini_home.join("antigravity-cli/conversations")],
        ),
        (
            "agy-ide",
            vec![gemini_home.join("antigravity-ide/conversations")],
        ),
    ] {
        let (mut files, mut present, mut failures) = (BTreeSet::new(), false, 0);
        let mut details = Vec::new();
        for dir in dirs {
            if !dir.exists() {
                continue;
            }
            present = true;
            for item in WalkDir::new(&dir)
                .max_depth(if entry == "codex" { usize::MAX } else { 1 })
                .follow_links(false)
                .into_iter()
                .filter_entry(|f| crate::discovery::ordinary(f.path()))
            {
                match item {
                    Ok(f) if f.file_type().is_file() => {
                        let n = f.file_name().to_string_lossy();
                        if (entry == "codex"
                            && n.starts_with("rollout-")
                            && (n.ends_with(".jsonl") || n.ends_with(".jsonl.zst")))
                            || (entry != "codex" && n.ends_with(".db"))
                        {
                            files.insert(f.into_path());
                        }
                    }
                    Err(_) => failures += 1,
                    _ => (),
                }
            }
        }
        if entry == "codex" {
            failures += crate::discovery::extend(c, codex_home, &mut files)?;
            present |= !files.is_empty();
        }
        let count = files.len();
        let (mut skipped, mut added) = (0, 0);
        let prior = setting(c, entry, Value::Null)?;
        let mut last_read = prior["last_read"].clone();
        for (i, p) in files.iter().enumerate() {
            progress(json!({"running":true,"entry":entry,"files":count,"done":i}));
            let read = (|| -> Result<(usize, usize)> {
                let key = identity((entry, p.to_string_lossy()));
                let sig = signature(p)?;
                if let Some(old) = c
                    .query_row(
                        "select skipped from files where id=? and sig=?",
                        params![key, sig],
                        |r| r.get::<_, usize>(0),
                    )
                    .optional()?
                {
                    return Ok((0, old));
                }
                let tx = c.transaction()?;
                let (changed, missed) = if entry == "codex" {
                    codex(&tx, p, &key)?
                } else {
                    agy::read(&tx, p, entry, &key)?
                };
                tx.execute(
                    "insert or replace into files values(?,?,?)",
                    params![key, sig, missed],
                )?;
                if changed > 0 {
                    bump(&tx)?
                }
                tx.commit()?;
                last_read = json!(now());
                Ok((changed, missed))
            })();
            match read {
                Ok((a, s)) => {
                    added += a;
                    skipped += s;
                    if s > 0 && details.len() < 30 {
                        details.push(json!([
                            p.file_name().unwrap_or_default().to_string_lossy(),
                            format!("{s} 条统计缺失、格式或身份无法核实")
                        ]))
                    }
                }
                Err(_) => {
                    failures += 1;
                    if details.len() < 30 {
                        details.push(json!([
                            p.file_name().unwrap_or_default().to_string_lossy(),
                            "来源文件读取失败"
                        ]))
                    }
                }
            }
        }
        save(
            c,
            entry,
            &json!({"files":count,"present":present,"checked":now(),"last_read":last_read,"added":added,"failures":failures,"skipped":skipped,"details":details,"readable":count.saturating_sub(failures),"health":if failures>0{"文件访问失败"}else if !present&&!last_read.is_null(){"来源失联"}else if !present{"未接入"}else if count==0{"目录为空"}else{"可读取"}}),
        )?;
    }
    Ok(())
}
