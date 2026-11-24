"""Run a portable ZIP with a fictional ledger on localhost. Requires Playwright and Chrome."""

import csv
import io
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import time
import zipfile
from contextlib import closing
from pathlib import Path
from urllib.request import Request, urlopen

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
ZIP = ROOT / "release/Meter-1.7-preview-Windows-x64.zip"
QA = ROOT / ".qa"


def run(folder):
    with zipfile.ZipFile(ZIP) as archive:
        assert not any("/private/" in n or "/.qa/" in n for n in archive.namelist())
        assert archive.testzip() is None
        archive.extractall(folder)
    app = folder / "AI用量账本"
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    def get(path):
        with urlopen(base + path, timeout=5) as reply:
            return json.load(reply)

    def launch():
        process = subprocess.Popen(
            [
                "cmd.exe",
                "/d",
                "/c",
                "启动账本.cmd",
                "--port",
                str(port),
                "--no-sync",
                "--no-browser",
            ],
            cwd=app,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(80):
            try:
                assert get("/api/instance")["version"] == "1.7-preview"
                return process
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("便携服务未能启动")

    def stop(process):
        if process.poll() is None:
            req = Request(base + "/api/stop", data=b"{}", headers={"X-Ledger-Local": "1"})
            with urlopen(req, timeout=5):
                pass
            process.wait(8)

    # Exercise first-launch migration through the ZIP launcher.
    private = app / "private"
    private.mkdir()
    with closing(sqlite3.connect(private / "ledger.sqlite3")) as legacy:
        legacy.executescript("""PRAGMA user_version=3;
            CREATE TABLE notes(id TEXT PRIMARY KEY,kind TEXT,day TEXT,entry TEXT,data TEXT,updated TEXT);
            INSERT INTO notes VALUES('fixture','subscription','2026-04-01','openai-api',
            '{"title":"虚构旧账","currency":"JPY","amount":"120","project":"虚构验收"}',NULL);""")
    proc = launch()
    try:
        assert get("/api/notes")[0]["title"] == "虚构旧账"
        assert len(list((private / "backups").glob("*.sqlite3"))) == 1
        rows = [
            dict(
                schema="meter.usage.v1",
                entry="openai-api",
                timestamp="2026-04-12T15:30:00Z",
                project="虚构验收",
                model="gpt-fictional",
                request_id=str(i),
                total=3,
                cache_read=0,
            )
            for i in range(62)
        ]
        rows[0]["cache_read"] = None
        path = folder / "虚构验收.json"
        path.write_text(json.dumps(rows), "utf-8")
        with sync_playwright() as playwright:
            binary = (
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
                / "Google/Chrome/Application/chrome.exe"
            )
            browser = playwright.chromium.launch(
                executable_path=str(binary), args=["--disable-background-networking"]
            )
            page = browser.new_page(viewport={"width": 1280, "height": 850}, accept_downloads=True)
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            unexpected = []

            def route(r):
                if r.request.url.startswith(base + "/"):
                    return r.continue_()
                unexpected.append(r.request.url)
                r.abort()

            page.route("**/*", route)
            page.goto(base + "/?tab=sources&period=all")
            page.locator("#import-file").set_input_files(path)
            expect(page.locator("#modal")).to_contain_text("新增 62")
            assert get("/api/view?period=all")["totals"]["n"] == 0
            page.locator("#apply-import").click()
            page.locator("#modal").wait_for(state="hidden")
            page.locator("[data-nav=home]").click()
            page.locator("[data-period=today]").click()
            page.locator("[data-period=all]").click()
            page.locator(".name-btn").first.wait_for()
            # A failed background refresh must preserve the visible scope and recover unattended.
            page.route("**/api/view?*", lambda route: route.abort(), times=1)
            price = dict(model="fixture-refresh", input=1, output=1, cache_read=0, cache_write=1)
            response = page.request.post(base + "/api/pricing", data=price, headers={"X-Ledger-Local": "1"})
            assert response.ok
            expect(page.locator("#health-warning")).to_contain_text("保留上次读取的数据", timeout=22000)
            expect(page.locator(".name-btn").first).to_contain_text("gpt-fictional")
            expect(page.locator("#health-warning")).not_to_contain_text("上次读取的数据", timeout=22000)
            page.locator("#show-breakdown").click()
            page.locator("[data-missing=cache_read]").click()
            expect(page.locator(".rank-qty")).to_contain_text("1/1")
            page.locator("#reset").click()
            page.locator(".sort-control .ts-control").click()
            page.locator(".ts-dropdown [data-value=net]:visible").click()
            page.locator("[data-expand]").first.click()
            expect(page.locator(".subrows")).to_contain_text("61/62")
            page.locator(".name-btn").first.click()
            expect(page.locator(".pager")).to_contain_text("1 / 2")
            page.locator("#next").click()
            expect(page.locator(".pager")).to_contain_text("2 / 2")
            for fmt in ("csv", "json"):
                with page.expect_download() as download:
                    page.locator(".export a").filter(has_text=fmt.upper()).first.click()
                b = Path(download.value.path()).read_bytes()
                result = (
                    json.loads(b)["records"]
                    if fmt == "json"
                    else list(csv.DictReader(io.StringIO(b.decode("utf-8-sig"))))
                )
                assert len(result) == 62 and sum(int(r["total"]) for r in result) == 186
                assert any(r["cache_read"] in (None, "") for r in result)
                assert any(r["cache_read"] in (0, "0") for r in result)
            page.locator("[data-evidence]").first.click()
            form = page.locator("#modal form")
            form.locator("[name=action]").select_option("account")
            form.locator("[name=value]").fill("虚构账号")
            form.locator("[name=reason]").fill("虚构账单核对")
            form.locator("button").click()
            expect(page.locator("#modal")).to_contain_text("虚构账号")
            page.locator("#modal [aria-label=关闭]").click()
            page.locator("#back").click()
            expect(page.locator("#sort")).to_have_value("net")
            page.locator(".name-btn").first.wait_for()
            assert "period=all" in page.url
            page.screenshot(path=str(QA / "reconciliation-desktop.png"))
            page.set_viewport_size({"width": 480, "height": 850})
            page.screenshot(path=str(QA / "reconciliation-narrow.png"))
            assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
            assert not errors, errors
            assert not unexpected, unexpected
            page.locator("[data-nav=sources]").click()
            page.locator(".management summary").click()
            page.locator("[data-batch]").first.click()
            page.locator("#next-line").click()
            expect(page.locator("#modal")).to_contain_text("第 62 行")
            page.locator("#modal [aria-label=关闭]").click()
            page.on("dialog", lambda dialog: dialog.accept())
            page.locator("[data-undo]").first.click()
            expect(page.locator("#toast")).to_contain_text("移除 62 条")
            page.locator("[data-nav=book]").click()
            page.locator("#add-note").click()
            form = page.locator("#note-form")
            form.locator("[name=media]").select_option("已验收代码任务")
            for key, value in (
                ("title", "虚构修复"),
                ("account", "虚构甲"),
                ("plan", "月计划"),
                ("produced", "2"),
                ("usable", "1"),
            ):
                form.locator("[name=" + key + "]").fill(value)
            form.locator("[type=submit]").click()
            expect(page.locator(".book-panel")).to_contain_text("虚构修复")
            page.locator("details:has(#book-filter) summary").click()
            page.locator("[name=book_account]").fill("虚构乙")
            page.locator("#book-filter .outline").click()
            expect(page.locator(".book-panel")).to_contain_text("尚无")
            page.locator("#clear-book").click()
            expect(page.locator(".book-panel")).to_contain_text("虚构甲")
            page.route("**/api/status", lambda route: route.abort())
            expect(page.locator("#health-warning")).to_contain_text("连接中断", timeout=22000)
            assert not errors, errors
            browser.close()
        stop(proc)
        proc = launch()
        assert get("/api/view?period=all")["totals"]["n"] == 0
        assert get("/api/manage")["batches"][0]["undone"]
        print(
            "ZIP startup, isolated offline UI, preview, date/sort, coverage, evidence edit, back, two-page exports, undo and restart: PASS"
        )
    finally:
        stop(proc)


if __name__ == "__main__":
    QA.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=QA) as directory:
        for key in ("TEMP", "TMP", "TMPDIR"):
            os.environ[key] = directory
        run(Path(directory))
