use std::{
    env, fs,
    path::{Path, PathBuf},
    process::Command,
};
fn assets(dir: &Path, root: &Path, arms: &mut String) {
    for file in fs::read_dir(dir).unwrap() {
        let path = file.unwrap().path();
        if path.is_dir() {
            assets(&path, root, arms);
            continue;
        }
        let key = path
            .strip_prefix(root)
            .unwrap()
            .to_string_lossy()
            .replace('\\', "/");
        let mime = match path.extension().and_then(|v| v.to_str()) {
            Some("js") => "text/javascript; charset=utf-8",
            Some("css") => "text/css",
            Some("html") => "text/html; charset=utf-8",
            Some("ttf") => "font/ttf",
            Some("svg") => "image/svg+xml",
            _ => "application/octet-stream",
        };
        arms.push_str(&format!(
            "{:?} => Some(({:?}, include_bytes!({:?}))),\n",
            if key == "index.html" {
                "/".into()
            } else {
                format!("/{key}")
            },
            mime,
            path.canonicalize().unwrap()
        ));
    }
}
fn main() {
    println!("cargo:rerun-if-changed=../web");
    println!("cargo:rerun-if-changed=../tools/MeterWidget.cs");
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let mut arms = String::from(
        "pub fn asset(path:&str)->Option<(&'static str,&'static [u8])>{match path {\n",
    );
    assets(Path::new("../web"), Path::new("../web"), &mut arms);
    arms.push_str("_=>None}}\n");
    fs::write(out.join("assets.rs"), arms).unwrap();
    let fx = PathBuf::from(env::var_os("WINDIR").expect("Windows x64 build host"))
        .join("Microsoft.NET/Framework64/v4.0.30319");
    let mut cmd = Command::new(fx.join("csc.exe"));
    cmd.args(["/nologo", "/optimize+", "/target:winexe"])
        .arg(format!("/out:{}", out.join("MeterWidget.exe").display()));
    for dll in [
        "System.Web.Extensions.dll",
        "System.Windows.Forms.dll",
        "System.Xaml.dll",
        "WPF/PresentationFramework.dll",
        "WPF/PresentationCore.dll",
        "WPF/WindowsBase.dll",
    ] {
        cmd.arg(format!("/reference:{}", fx.join(dll).display()));
    }
    assert!(
        cmd.arg(r"..\tools\MeterWidget.cs")
            .status()
            .unwrap()
            .success(),
        "WPF component build failed"
    );
}
