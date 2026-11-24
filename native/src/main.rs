#![windows_subsystem = "windows"]
mod agy;
mod collect;
mod db;
mod discovery;
mod imports;
mod manage;
mod query;
mod quota;
mod server;
mod subscriptions;
use anyhow::{bail, ensure, Context, Result};
use serde_json::{json, Value};
use server::App;
use std::{
    env, fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::Duration,
};
use tiny_http::Server;
#[link(name = "user32")]
extern "system" {
    fn MessageBoxW(w: usize, t: *const u16, c: *const u16, flags: u32) -> i32;
}
#[link(name = "shell32")]
extern "system" {
    fn ShellExecuteW(
        w: usize,
        op: *const u16,
        file: *const u16,
        args: *const u16,
        dir: *const u16,
        show: i32,
    ) -> isize;
}
#[link(name = "kernel32")]
extern "system" {
    fn CreateMutexW(sec: usize, owner: i32, name: *const u16) -> usize;
    fn GetLastError() -> u32;
    fn CloseHandle(h: usize) -> i32;
}
struct Guard(usize);
impl Drop for Guard {
    fn drop(&mut self) {
        unsafe {
            CloseHandle(self.0);
        }
    }
}
fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain([0]).collect()
}
fn browser(port: u16) -> Result<()> {
    let opened = unsafe {
        ShellExecuteW(
            0,
            wide("open").as_ptr(),
            wide(&format!("http://127.0.0.1:{port}/")).as_ptr(),
            std::ptr::null(),
            std::ptr::null(),
            1,
        )
    };
    ensure!(
        opened > 32,
        "无法打开默认浏览器，请访问 http://127.0.0.1:{port}/"
    );
    Ok(())
}
fn request(port: u16, path: &str, post: bool) -> Result<Value> {
    let mut s =
        TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(400))?;
    s.set_read_timeout(Some(Duration::from_secs(3)))?;
    write!(s,"{} {path} HTTP/1.0\r\nHost: 127.0.0.1:{port}\r\nX-Ledger-Local: 1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",if post{"POST"}else{"GET"})?;
    let mut out = String::new();
    s.take(32768).read_to_string(&mut out)?;
    Ok(serde_json::from_str(
        out.split_once("\r\n\r\n").context("本地服务响应无效")?.1,
    )?)
}
fn run() -> Result<()> {
    let mut dir = env::current_exe()?
        .parent()
        .context("程序目录无效")?
        .join("rust-private");
    let home = env::var_os("USERPROFILE")
        .map(PathBuf::from)
        .context("缺少用户目录")?;
    let mut codex = env::var_os("CODEX_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| home.join(".codex"));
    let mut gemini = home.join(".gemini");
    let (mut port, mut widget, mut no_browser, mut auto) = (17963u16, false, false, true);
    let mut args = env::args_os().skip(1);
    while let Some(a) = args.next() {
        match a.to_str().unwrap_or("") {
            "--data-dir" => dir = args.next().context("缺少目录")?.into(),
            "--codex-home" => codex = args.next().context("缺少目录")?.into(),
            "--gemini-home" => gemini = args.next().context("缺少目录")?.into(),
            "--port" => port = args.next().context("缺少端口")?.to_string_lossy().parse()?,
            "--no-browser" => no_browser = true,
            "--no-sync" => auto = false,
            "--widget" => widget = true,
            _ => bail!("未知参数：{}", a.to_string_lossy()),
        }
    }
    fs::create_dir_all(&dir).context("程序旁需要可写目录，可用 --data-dir 指定")?;
    let dir = dir.canonicalize()?;
    let file = dir.join("native-ledger.sqlite3");
    let id = db::identity(file.to_string_lossy().to_lowercase());
    let handle = unsafe { CreateMutexW(0, 0, wide(&format!("Local\\KeduRust-{id}")).as_ptr()) };
    let existing = unsafe { GetLastError() } == 183;
    ensure!(handle != 0, "无法锁定本地账本");
    let _guard = Guard(handle);
    if existing {
        for _ in 0..40 {
            if let Some(p) = fs::read_to_string(dir.join("port"))
                .ok()
                .and_then(|v| v.parse::<u16>().ok())
            {
                if request(p, "/api/instance", false)
                    .is_ok_and(|v| v["instance"] == id && v["app"] == "kedu-native")
                {
                    if widget {
                        ensure!(
                            request(p, "/api/widget/open", true)?["ok"] == true,
                            "小组件启动失败"
                        )
                    } else if !no_browser {
                        browser(p)?
                    }
                    return Ok(());
                }
            }
            thread::sleep(Duration::from_millis(125))
        }
        bail!("同一 Rust 账本正在启动或未响应，请稍后重试")
    }
    let listener = (port..=port.saturating_add(15))
        .find_map(|p| TcpListener::bind(("127.0.0.1", p)).ok())
        .context("本地端口被占用")?;
    let port = listener.local_addr()?.port();
    let server =
        Arc::new(Server::from_listener(listener, None).map_err(|e| anyhow::anyhow!("{e}"))?);
    db::init(&file)?;
    fs::write(dir.join("port"), port.to_string())?;
    let app = Arc::new(App {
        db: file,
        codex,
        gemini,
        port,
        id,
        scan: Mutex::new(json!({"running":false})),
        stop: AtomicBool::new(false),
        quota_running: Mutex::new(false),
        widget_cache: Mutex::new(None),
    });
    let mut workers = vec![];
    for _ in 0..6 {
        let a = app.clone();
        let s = server.clone();
        workers.push(thread::spawn(move || {
            while !a.stop.load(Ordering::Acquire) {
                if let Ok(Some(r)) = s.recv_timeout(Duration::from_millis(200)) {
                    server::handle(&a, r)
                }
            }
        }))
    }
    if auto {
        app.sync()
    }
    if widget {
        app.widget("")?
    } else if !no_browser {
        browser(port)?
    }
    let mut last = std::time::Instant::now();
    while !app.stop.load(Ordering::Acquire) {
        thread::sleep(Duration::from_millis(150));
        if auto && last.elapsed() >= Duration::from_secs(30) {
            app.sync();
            last = std::time::Instant::now();
        }
    }
    for w in workers {
        let _ = w.join();
    }
    Ok(())
}
fn main() {
    if let Err(e) = run() {
        let msg = format!("{e:#}");
        if env::args_os().any(|s| s == "--no-browser") {
            eprintln!("{msg}")
        } else {
            unsafe {
                MessageBoxW(
                    0,
                    wide(&msg).as_ptr(),
                    wide("刻度 Rust · 启动失败").as_ptr(),
                    16,
                );
            }
        }
        std::process::exit(1)
    }
}
