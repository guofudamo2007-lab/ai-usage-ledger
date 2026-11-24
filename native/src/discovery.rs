use crate::db::*;
use anyhow::{ensure, Context, Result};
use rusqlite::{params, Connection, OpenFlags};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{BufRead, BufReader, Read},
    os::windows::fs::MetadataExt,
    path::{Path, PathBuf},
};
use walkdir::WalkDir;

pub fn ordinary(p: &Path) -> bool {
    p.symlink_metadata()
        .is_ok_and(|m| m.file_attributes() & 0x400 == 0)
}
fn file_id(p: &Path) -> Option<String> {
    let name = p
        .file_name()?
        .to_str()?
        .trim_end_matches(".zst")
        .strip_suffix(".jsonl")?;
    let id = name.get(name.len().checked_sub(36)?..)?;
    id.bytes()
        .enumerate()
        .all(|(i, c)| {
            if [8, 13, 18, 23].contains(&i) {
                c == b'-'
            } else {
                c.is_ascii_hexdigit()
            }
        })
        .then(|| id.to_lowercase())
}
pub fn readonly(p: &Path) -> Result<Connection> {
    let p = p.canonicalize()?;
    let w = PathBuf::from(format!("{}-wal", p.display()));
    let live = w.metadata().is_ok_and(|m| m.len() > 0);
    ensure!(
        !live || PathBuf::from(format!("{}-shm", p.display())).is_file(),
        "WAL 暂不可只读访问"
    );
    let name = p.to_string_lossy();
    let s = name
        .trim_start_matches(r"\\?\")
        .replace('\\', "/")
        .replace('%', "%25")
        .replace('?', "%3F")
        .replace('#', "%23");
    Ok(Connection::open_with_flags(
        format!(
            "file:///{s}?mode=ro{}",
            if live { "" } else { "&immutable=1" }
        ),
        OpenFlags::SQLITE_OPEN_URI | OpenFlags::SQLITE_OPEN_READ_ONLY,
    )?)
}
fn index(root: &Path) -> Result<BTreeMap<String, (String, Option<i64>)>> {
    let p = fs::read_dir(root)?
        .filter_map(Result::ok)
        .filter_map(|f| {
            let p = f.path();
            let n = p
                .file_name()?
                .to_str()?
                .strip_prefix("state_")?
                .strip_suffix(".sqlite")?
                .parse::<u32>()
                .ok()?;
            ordinary(&p).then_some((n, p))
        })
        .max_by_key(|(n, _)| *n)
        .context("无本机索引")?
        .1;
    let src = readonly(&p)?;
    let mut st = src.prepare("select id, rollout_path, tokens_used from threads")?;
    let all = st
        .query_map([], |r| {
            Ok((
                r.get::<_, String>(0)?.to_lowercase(),
                (
                    r.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    r.get::<_, Option<i64>>(2)?.filter(|n| *n >= 0),
                ),
            ))
        })?
        .collect::<rusqlite::Result<_>>()?;
    Ok(all)
}
fn verify(path: &Path, id: &str) -> Result<bool> {
    let f = fs::File::open(path)?;
    let stream: Box<dyn Read> = if path.extension().is_some_and(|x| x == "zst") {
        Box::new(zstd::Decoder::new(f)?)
    } else {
        Box::new(f)
    };
    let mut line = vec![];
    BufReader::new(stream.take(16 * 1024 * 1024 + 1)).read_until(b'\n', &mut line)?;
    ensure!(line.ends_with(b"\n"), "未能验证身份");
    let v: Value = serde_json::from_slice(&line)?;
    Ok(v["type"] == "session_meta"
        && v["payload"]["id"]
            .as_str()
            .or(v["payload"]["session_id"].as_str())
            .is_some_and(|s| s.eq_ignore_ascii_case(id)))
}
pub fn set_backup(c: &Connection, path: &str) -> Result<()> {
    if !path.is_empty() {
        let p = Path::new(path);
        ensure!(
            p.is_absolute()
                && p.is_dir()
                && ordinary(p)
                && !path.starts_with(r"\\")
                && !path.starts_with("//"),
            "备份目录须为本机实际目录"
        )
    }
    save(c, "backup", &json!(path))
}
pub fn extend(c: &mut Connection, root: &Path, paths: &mut BTreeSet<PathBuf>) -> Result<usize> {
    let index = index(root);
    let mut warnings = Vec::new();
    let mut failed = 0;
    if index.is_err() {
        warnings.push("没有可用的线程索引；已扫描本机会话和归档")
    }
    let index = index.unwrap_or_default();
    let mut located: BTreeSet<_> = paths.iter().filter_map(|p| file_id(p)).collect();
    let root_abs = root.canonicalize().ok();
    let other_dirs: Vec<PathBuf> = root
        .parent()
        .and_then(|p| fs::read_dir(p).ok())
        .into_iter()
        .flatten()
        .filter_map(Result::ok)
        .filter(|f| {
            f.file_name().to_string_lossy().starts_with(".codex")
                && f.path().join("sessions").is_dir()
        })
        .filter_map(|f| f.path().canonicalize().ok())
        .filter(|p| Some(p) != root_abs.as_ref())
        .collect();
    let tx = c.transaction()?;
    for (uid, (_, total)) in &index {
        tx.execute("insert into thread_index values(?,?,0) on conflict(id) do update set total=coalesce(excluded.total,total)",params![identity(("codex-thread",uid)),total])?;
    }
    let mut check = |p: PathBuf, id: &str| -> Result<()> {
        if !p.is_file() || !ordinary(&p) || paths.contains(&p) {
            return Ok(());
        }
        let canonical = p.canonicalize()?;
        if other_dirs.iter().any(|r| canonical.starts_with(r)) {
            return Ok(());
        }
        if !index.contains_key(id)
            && !tx.query_row(
                "select exists(select 1 from thread_index where id=?)",
                [identity(("codex-thread", id))],
                |r| r.get::<_, bool>(0),
            )?
        {
            return Ok(());
        }
        if verify(&p, id).unwrap_or(false) {
            located.insert(id.into());
            paths.insert(p);
        } else {
            failed += 1
        }
        Ok(())
    };
    for (uid, (path, _)) in &index {
        let p = Path::new(path);
        if !p.is_absolute() || root_abs.is_none() {
            continue;
        }
        for p in [p.to_owned(), PathBuf::from(format!("{path}.zst"))] {
            if p.canonicalize()
                .is_ok_and(|l| l.starts_with(root_abs.as_ref().unwrap()))
            {
                check(p, uid)?
            }
        }
    }
    let backup = setting(&tx, "backup", json!(""))?;
    let mut dirs = vec![root.join("backups"), root.join("rollout-migrations")];
    if let Some(s) = backup.as_str().filter(|s| !s.is_empty()) {
        dirs.push(s.into())
    }
    for dir in dirs {
        if !dir.is_dir() {
            continue;
        }
        for f in WalkDir::new(dir)
            .into_iter()
            .filter_entry(|f| {
                ordinary(f.path())
                    && !other_dirs
                        .iter()
                        .any(|r| f.path().canonicalize().is_ok_and(|p| p.starts_with(r)))
            })
            .filter_map(Result::ok)
        {
            if f.file_type().is_file() {
                if let Some(id) = file_id(f.path()) {
                    check(f.into_path(), &id)?
                }
            }
        }
    }
    tx.execute("update thread_index set located=0 where located=1", [])?;
    for uid in located {
        tx.execute(
            "update thread_index set located=1 where id=?",
            [identity(("codex-thread", uid))],
        )?;
    }
    if failed > 0 {
        warnings.push("部分备份日志未能核实会话身份")
    }
    save(
        &tx,
        "last_search",
        &json!({"warnings":warnings,"checked_backup":backup}),
    )?;
    tx.commit()?;
    Ok(failed)
}
pub fn status(c: &Connection, root: &Path) -> Result<Value> {
    let mut v=crate::manage::rows(c,"select count(*) threads,count(total) counted,sum(total) index_total,count(case when located=0 then 1 end) missing,count(case when located=0 then total end) known,sum(case when located=0 then total end) total from thread_index",[])?.pop().unwrap();
    v["home"] = json!(root);
    v["backup"] = setting(c, "backup", json!(""))?;
    v["last"] = setting(c, "last_search", Value::Null)?;
    Ok(v)
}
