use crate::db::*;
use anyhow::{ensure, Context, Result};
use rusqlite::Connection;
use serde_json::{json, Value};
use std::{
    env, fs,
    io::{BufRead, BufReader, Read},
    os::windows::process::CommandExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::mpsc,
    thread,
    time::Duration,
};
pub const READERS: [&str; 5] = ["copilot", "minimax", "grok", "claude-code", "kimi-code"];
fn grok_path() -> PathBuf {
    env::var_os("GROK_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            PathBuf::from(env::var_os("USERPROFILE").unwrap_or_default()).join(".grok")
        })
        .join("logs/unified.jsonl")
}
fn binary(name: &str) -> Option<PathBuf> {
    env::split_paths(&env::var_os("PATH").unwrap_or_default())
        .filter(|p| p.is_absolute())
        .map(|p| p.join(name))
        .find(|p| p.is_file())
}
fn mmx() -> Option<(PathBuf, Vec<String>)> {
    if let Some(p) = binary("mmx.exe") {
        return Some((p, vec![]));
    }
    let node = binary("node.exe")?;
    let script = env::split_paths(&env::var_os("PATH")?)
        .filter(|p| p.is_absolute())
        .map(|p| p.join("node_modules/mmx-cli/dist/mmx.mjs"))
        .find(|p| p.is_file())?;
    Some((node, vec![script.to_string_lossy().into_owned()]))
}
pub fn available(k: &str, dir: &Path) -> bool {
    match k {
        "copilot" => binary("gh.exe").is_some(),
        "minimax" => mmx().is_some(),
        "grok" => grok_path().is_file(),
        "claude-code" | "kimi-code" => dir
            .join("quota-snapshots")
            .join(format!("{k}.json"))
            .is_file(),
        _ => false,
    }
}
pub fn catalog(c: &Connection, dir: &Path) -> Result<Value> {
    let mut v = json!({});
    for (k, data) in CATALOG["services"].as_object().unwrap() {
        v[k] = json!({"name":data[0],"category":data[1],"url":data[2],"hint":data[3],"automatic":available(k,dir),"importable":matches!(k.as_str(),"claude-code"|"kimi-code"),"saved":!setting(c,&format!("subscription:{k}"),Value::Null)?.is_null()});
    }
    Ok(v)
}
fn json_command(cmd: &mut Command) -> Result<Value> {
    let mut child = cmd
        .creation_flags(0x08000000)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let out = child.stdout.take().unwrap();
    let (tx, rx) = mpsc::channel();
    thread::spawn(move || {
        let mut buf = vec![];
        let res = out.take(1024 * 1024 + 1).read_to_end(&mut buf);
        let _ = tx.send((res, buf));
    });
    let res = rx.recv_timeout(Duration::from_secs(20));
    if res.is_err() {
        let _ = child.kill();
        let _ = child.wait();
        anyhow::bail!("额度查询超时")
    }
    let (read, buf) = res?;
    read?;
    let deadline = std::time::Instant::now() + Duration::from_secs(3);
    loop {
        if let Some(s) = child.try_wait()? {
            ensure!(s.success(), "客户端额度查询失败");
            break;
        }
        if std::time::Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            anyhow::bail!("客户端未结束")
        }
        thread::sleep(Duration::from_millis(50))
    }
    ensure!(buf.len() <= 1024 * 1024, "额度报表过大");
    Ok(serde_json::from_slice(&buf)?)
}
fn pct(v: &Value) -> Option<f64> {
    v.as_f64().filter(|x| (0.0..=100.).contains(x))
}
fn epoch(v: &Value) -> Option<i64> {
    v.as_str()
        .and_then(|s| chrono::DateTime::parse_from_rfc3339(s).ok())
        .map(|d| d.timestamp())
}
fn grok() -> Result<Value> {
    let mut reader = BufReader::new(fs::File::open(grok_path())?);
    let mut buf = Vec::new();
    let mut last = None;
    loop {
        buf.clear();
        if (&mut reader)
            .take(1024 * 1024 + 1)
            .read_until(b'\n', &mut buf)?
            == 0
        {
            break;
        }
        if !buf.ends_with(b"\n") && buf.len() > 1024 * 1024 {
            reader.skip_until(b'\n')?;
            continue;
        }
        if !buf
            .windows(30)
            .any(|b| b == b"billing: fetched credits config")
        {
            continue;
        }
        let Ok(v) = serde_json::from_slice::<Value>(&buf) else {
            continue;
        };
        if v["msg"] != "billing: fetched credits config" {
            continue;
        }
        let a = &v["ctx"]["config"];
        let u = pct(&a["creditUsagePercent"]).context("额度字段无效")?;
        ensure!(epoch(&v["ts"]).is_some(), "快照缺少时间");
        last = Some(
            json!({"source":"client-log","at":v["ts"],"windows":[{"label":if a["currentPeriod"]["type"]=="USAGE_PERIOD_TYPE_WEEKLY"{"每周额度"}else{"当前周期"},"remaining":100.-u,"reset":epoch(&a["currentPeriod"]["end"])}]}),
        );
    }
    last.context("没有可读取的 Grok 额度快照")
}
pub fn snapshot(
    c: &Connection,
    dir: &Path,
    k: &str,
    write: bool,
    force: bool,
    import: Option<&Value>,
) -> Result<Value> {
    ensure!(READERS.contains(&k), "此订阅仅可手工记账");
    let key = format!("subscription:{k}");
    let old = setting(c, &key, Value::Null)?;
    if !write {
        return Ok(old);
    }
    if import.is_none()
        && !force
        && old["checked"]
            .as_i64()
            .is_some_and(|s| (0..300).contains(&(chrono::Utc::now().timestamp() - s)))
    {
        return Ok(old);
    }
    let mut windows = vec![];
    let mut source = k;
    let mut at = now();
    let mut res = match k {
        "grok" => grok()?,
        "copilot" => {
            let v = json_command(
                Command::new(binary("gh.exe").context("缺少 GitHub CLI")?).args([
                    "api",
                    "--hostname",
                    "github.com",
                    "/copilot_internal/user",
                    "--jq",
                    "{quota_snapshots,quota_reset_date_utc}",
                ]),
            )?;
            for (label, w) in v["quota_snapshots"].as_object().context("没有额度快照")? {
                if w["unlimited"] == true {
                    windows
                        .push(json!({"label":label,"state":"不限量","remaining":null,"reset":null}))
                } else if w["has_quota"] != false {
                    if let Some(rem) = pct(&w["percent_remaining"]) {
                        windows.push(json!({"label":label,"remaining":rem,"reset":w["quota_reset_at"].as_i64().or_else(||epoch(&v["quota_reset_date_utc"]))}))
                    }
                }
            }
            Value::Null
        }
        "minimax" => {
            let (exe, args) = mmx().context("缺少 MiniMax CLI")?;
            let v = json_command(
                Command::new(exe)
                    .args(args)
                    .args(["quota", "show", "--output", "json"]),
            )?;
            for m in v["model_remains"].as_array().context("未返回套餐额度")? {
                let Some(model) = m["model_name"].as_str() else {
                    continue;
                };
                if ["interval", "weekly"].iter().all(|p| {
                    m[format!("current_{p}_status")] == 3
                        && m[format!("current_{p}_total_count")] == 0
                }) {
                    continue;
                }
                for (p, label, end) in [
                    ("interval", "当前窗口", "end_time"),
                    ("weekly", "每周", "weekly_end_time"),
                ] {
                    let name = format!("{model} · {label}");
                    if p == "weekly" && m["current_weekly_status"] == 3 {
                        windows.push(
                            json!({"label":name,"remaining":null,"reset":null,"state":"不限量"}),
                        )
                    } else if let Some(rem) = pct(&m[format!("current_{p}_remaining_percent")]) {
                        let boost = if p == "weekly" {
                            m["weekly_boost_permille"].as_u64().unwrap_or(1000)
                        } else {
                            1000
                        };
                        windows.push(json!({"label":name,"remaining":rem*boost as f64/1000.,"reset":m[end].as_i64().map(|ms|ms/1000)}))
                    }
                }
            }
            Value::Null
        }
        _ => {
            let file;
            let v = if let Some(v) = import {
                source = "import";
                v
            } else {
                source = "file";
                let f = fs::File::open(dir.join("quota-snapshots").join(format!("{k}.json")))
                    .context("缺少额度快照文件")?;
                ensure!(f.metadata()?.len() <= 1024 * 1024, "快照过大");
                at = chrono::DateTime::<chrono::Utc>::from(f.metadata()?.modified()?).to_rfc3339();
                file = serde_json::from_reader(f.take(1024 * 1024 + 1))?;
                &file
            };
            if k == "claude-code" {
                for (f, label) in [("five_hour", "5 小时"), ("seven_day", "每周")] {
                    let w = &v["rate_limits"][f];
                    if let Some(u) = pct(&w["used_percentage"]) {
                        windows.push(json!({"label":label,"remaining":100.-u,"reset":w["resets_at"].as_i64()}))
                    }
                }
            } else {
                let v = v.get("data").unwrap_or(v);
                ensure!(v["kind"] == "ok", "Kimi 快照无效");
                for (f, label) in [
                    ("limit5h", "5 小时"),
                    ("limit7d", "每周"),
                    ("monthTotal", "月总额度"),
                    ("monthCode", "月编程额度"),
                ] {
                    let w = &v["quota"]["usages"][f];
                    if let Some(u) = w["usedRatio"].as_f64().filter(|x| (0.0..=1.).contains(x)) {
                        windows.push(json!({"label":label,"remaining":100.*(1.-u),"reset":epoch(&w["resetAt"])}))
                    }
                }
            }
            Value::Null
        }
    };
    if res.is_null() {
        ensure!(!windows.is_empty(), "未找到可核实的额度字段");
        res = json!({"source":source,"at":at,"windows":windows})
    }
    res["checked"] = json!(chrono::Utc::now().timestamp());
    save(c, &key, &res)?;
    Ok(res)
}
