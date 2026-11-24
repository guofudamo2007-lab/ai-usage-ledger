use crate::db::*;
use anyhow::{ensure, Context, Result};
use serde_json::{json, Value};
use std::{
    env,
    io::{BufRead, BufReader, Read, Write},
    os::windows::process::CommandExt,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::mpsc,
    thread,
    time::Duration,
};
fn binary(name: &str) -> Result<PathBuf> {
    let paths = env::var_os("PATH").unwrap_or_default();
    let mut dirs: Vec<_> = env::split_paths(&paths)
        .filter(|p| p.is_absolute())
        .collect();
    if let Some(p) = env::var_os("LOCALAPPDATA") {
        dirs.push(PathBuf::from(p).join(if name == "codex.exe" {
            "Programs/OpenAI/Codex/bin"
        } else {
            "agy/bin"
        }))
    }
    dirs.iter()
        .map(|p| p.join(name))
        .find(|p| p.is_file())
        .context("未找到已安装的官方 CLI")
}
pub fn live(provider: &str, home: &Path, dir: &Path) -> Result<Value> {
    let codex = provider == "codex";
    let mut cmd = Command::new(binary(if codex { "codex.exe" } else { "agy.exe" })?);
    if codex {
        ensure!(home.is_dir(), "Codex 目录不存在");
        cmd.arg("app-server").env("CODEX_HOME", home);
    } else {
        cmd.args([
            "-p",
            "/usage",
            "--output-format",
            "json",
            "--print-timeout",
            "20s",
        ]);
    }
    let mut child = cmd
        .current_dir(dir)
        .creation_flags(0x08000000)
        .stdin(if codex { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()?;
    let input = child.stdin.take();
    let output = child.stdout.take().unwrap();
    let (tx, rx) = mpsc::channel();
    let reader = thread::spawn(move || {
        let res = (|| -> Result<Vec<Value>> {
            let stream = BufReader::new(output.take(1024 * 1024));
            let mut windows = vec![];
            if let Some(mut pipe) = input {
                writeln!(
                    pipe,
                    "{}",
                    json!({"id":1,"method":"initialize","params":{"clientInfo":{"name":"kedu-rust","version":"1"}}})
                )?;
                for line in stream.lines() {
                    let msg: Value = serde_json::from_str(&line?)?;
                    if msg["id"] == 1 {
                        ensure!(msg.get("result").is_some(), "Codex 初始化失败");
                        writeln!(pipe, "{}", json!({"method":"initialized"}))?;
                        writeln!(
                            pipe,
                            "{}",
                            json!({"id":2,"method":"account/rateLimits/read","params":null})
                        )?;
                    }
                    if msg["id"] == 2 {
                        let result = &msg["result"];
                        let b = result["rateLimitsByLimitId"]
                            .get("codex")
                            .unwrap_or(&result["rateLimits"]);
                        ensure!(
                            b["limitId"].is_null() || b["limitId"] == "codex",
                            "窗口归属未知"
                        );
                        for key in ["primary", "secondary"] {
                            let w = &b[key];
                            if let Some(u) = w["usedPercent"]
                                .as_f64()
                                .filter(|x| (0.0..=100.).contains(x))
                            {
                                windows.push(json!({"slot":key,"remaining":100.-u,"minutes":w["windowDurationMins"].as_i64().filter(|x|*x>0),"reset":w["resetsAt"].as_i64().filter(|x|*x>0)}))
                            }
                        }
                        break;
                    }
                }
            } else {
                let report: Value = serde_json::from_reader(stream)?;
                ensure!(
                    report["status"] == "SUCCESS" && report["num_turns"] == 0,
                    "AGY 未返回额度报表"
                );
                for line in report["response"]
                    .as_str()
                    .context("额度报表无效")?
                    .trim()
                    .lines()
                {
                    let f: Vec<_> = line.split('\t').map(str::trim).collect();
                    ensure!((3..=4).contains(&f.len()), "额度报表格式无效");
                    let r: f64 = f[2].strip_suffix('%').context("缺少百分比")?.parse()?;
                    ensure!((0.0..=100.).contains(&r), "额度百分比无效");
                    let reset = f
                        .get(3)
                        .filter(|s| !s.is_empty())
                        .map(|s| chrono::DateTime::parse_from_rfc3339(s).map(|d| d.timestamp()))
                        .transpose()?;
                    windows.push(json!({"group":f[0],"label":f[1],"remaining":r,"reset":reset}));
                }
            }
            ensure!(!windows.is_empty(), "本次未返回可核实的额度窗口");
            Ok(windows)
        })();
        let _ = tx.send(res);
    });
    let result = rx.recv_timeout(Duration::from_secs(if codex { 18 } else { 35 }));
    let _ = child.kill();
    let _ = child.wait();
    if result.is_ok() {
        let _ = reader.join();
    }
    Ok(
        json!({"at":now(),"source":if codex{"rpc"}else{"agy-cli"},"windows":result.context("额度查询超时")??}),
    )
}
