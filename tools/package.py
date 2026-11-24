"""Assemble a relocatable CPython runtime and a clean Windows ZIP using allowlists."""

import argparse
import tempfile
import importlib.util
import shutil
import os
import subprocess
import sys
import sysconfig
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def runtime(tz):
    if sys.version_info[:2] != (3, 14) or sysconfig.get_platform() != "win-amd64":
        raise SystemExit("请用 Windows x64 CPython 3.14 构建，Zstandard 由标准库提供。")
    base, target = Path(sys.base_prefix), ROOT / "runtime"
    target.mkdir(exist_ok=True)
    for name in (
        "python.exe",
        "python314.dll",
        "python3.dll",
        "vcruntime140.dll",
        "vcruntime140_1.dll",
        "LICENSE.txt",
    ):
        shutil.copy2(base / name, target / name)
    for src in (base / "DLLs").iterdir():
        if src.suffix.lower() in (".pyd", ".dll") and src.name not in (
            "_tkinter.pyd",
            "tk86t.dll",
            "tcl86t.dll",
            "_remote_debugging.pyd",
        ):
            shutil.copy2(src, target / src.name)
    lib = base / "Lib"
    excluded = {
        "site-packages",
        "test",
        "tests",
        "idlelib",
        "tkinter",
        "turtledemo",
        "ensurepip",
        "__pycache__",
    }
    with zipfile.ZipFile(target / "python314.zip", "w", zipfile.ZIP_DEFLATED) as out:
        for src in lib.rglob("*.py"):
            rel = src.relative_to(lib)
            if not set(rel.parts) & excluded:
                out.write(src, rel.as_posix())
    if not tz:
        spec = importlib.util.find_spec("tzdata")
        if not spec:
            raise SystemExit("请用 --tzdata 指定已安装的 tzdata 包目录")
        tz = Path(spec.origin).parent
    shutil.copytree(
        tz,
        target / "tzdata",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("*.pyc", "__pycache__"),
    )
    for lic in tz.parent.glob("tzdata-*.dist-info/licenses"):
        shutil.copytree(lic, target / "tzdata-licenses", dirs_exist_ok=True)
    (target / "python314._pth").write_text("python314.zip\n.\n..\n", "ascii")


def archive():
    dest = ROOT / "release"
    dest.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest) as folder:
        assemble(Path(folder), dest)


def assemble(build, dest):
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    references = [
        "System.Web.Extensions.dll",
        "System.Windows.Forms.dll",
        "System.Xaml.dll",
        "WPF/PresentationFramework.dll",
        "WPF/PresentationCore.dll",
        "WPF/WindowsBase.dll",
    ]
    subprocess.run(
        [
            str(framework / "csc.exe"),
            "/nologo",
            "/optimize+",
            "/target:winexe",
            "/out:" + str(build / "MeterWidget.exe"),
            *["/reference:" + str(framework / name) for name in references],
            str(ROOT / "tools/MeterWidget.cs"),
        ],
        check=True,
    )
    for required in (
        "LICENSE",
        "runtime/python.exe",
        "runtime/python314.zip",
        "runtime/LICENSE.txt",
        "runtime/tzdata/zoneinfo/UTC",
        "runtime/tzdata-licenses/LICENSE",
    ):
        if not (ROOT / required).is_file():
            raise SystemExit(
                f"缺少 {required}。请先准备运行环境，可用 --runtime --archive 一起构建。"
            )
    archive_path = build / "Meter-1.7-preview-Windows-x64.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as out:
        for rel in [
            "LICENSE",
            "启动账本.cmd",
            "MeterWidget.exe",
            "使用说明.md",
            "ledger_app.py",
            "ledger",
            "runtime",
            "web",
        ]:
            src = (build if rel == "MeterWidget.exe" else ROOT) / rel
            candidates = [src] if src.is_file() else sorted(src.rglob("*"))
            for p in candidates:
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                    out.write(
                        p,
                        (
                            Path("AI用量账本") / (rel if src.is_file() else p.relative_to(ROOT))
                        ).as_posix(),
                    )
    final = archive_path.replace(dest / archive_path.name)
    print(final, final.stat().st_size)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", action="store_true")
    ap.add_argument("--archive", action="store_true")
    ap.add_argument("--tzdata", type=Path)
    args = ap.parse_args()
    if args.runtime:
        runtime(args.tzdata)
    if args.archive:
        archive()
