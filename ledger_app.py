"""Windows local usage ledger. Standard library only, no outbound data connections."""

import argparse
import csv
import hashlib
import io
import json
import socket
import sqlite3
import subprocess
import threading
import time
import webbrowser
from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ledger import evidence, agy_quota, appearance, codex_search, manual, pricing, query, quota, subscriptions
from ledger.collect import sync
from ledger.core import ENTRIES, FIELDS, connect, digest, initialize
from ledger.imports import import_file, undo

ROOT = Path(__file__).resolve().parent
PORT = 17863
VERSION = "1.7-preview"


def code_build():
    h = hashlib.sha256()
    for path in [Path(__file__).resolve(), *sorted((ROOT / "ledger").glob("*.py"))]:
        h.update(path.name.encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


BUILD = code_build()
STATIC = {
    "/": ("index.html", "text/html"),
    "/style.css": ("style.css", "text/css"),
    "/app.js": ("app.js", "text/javascript"),
    "/mark.svg": ("mark.svg", "image/svg+xml"),
    "/fonts/IBMPlexMono-Medium.ttf": ("fonts/IBMPlexMono-Medium.ttf", "font/ttf"),
}
for asset, mime in (("uplot.js", "text/javascript"), ("tom-select.js", "text/javascript"), ("components.css", "text/css"), ("icons.svg", "image/svg+xml")):
    STATIC[f"/vendor/{asset}"] = (f"vendor/{asset}", mime)
for module in ("ui", "books", "instruments", "reference-data", "references", "appearance", "review", "pricing"):
    STATIC[f"/{module}.js"] = (f"{module}.js", "text/javascript")


def open_widget(port, database, zone=None):
    binary = ROOT / "MeterWidget.exe"
    if not binary.is_file():
        raise ValueError("缺少 MeterWidget.exe，请解压完整便携包")
    subprocess.Popen([str(binary), str(port), zone or "", str(database.resolve().parent / "widget-window.json")], cwd=ROOT)


class Collector:
    def __init__(self, database):
        self.file = database
        self.lock = threading.Lock()
        self.last_done = 0.0
        self.state = {"running": False, "finished": None}

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def snapshot(self):
        with self.lock:
            return dict(self.state)

    def start(self):
        with self.lock:
            if self.state["running"]:
                return
            self.state.update(running=True, error=None, done=0, files=0, entry=None)
        threading.Thread(target=self.run, daemon=True).start()

    def maybe_start(self, min_gap=8.0):
        with self.lock:
            if self.state["running"] or (self.last_done and time.monotonic() - self.last_done < min_gap):
                return False
            self.state.update(running=True, error=None, done=0, files=0, entry=None)
        threading.Thread(target=self.run, daemon=True).start()
        return True

    def run(self):
        try:
            self.update(report=sync(self.file, self.update))
        except Exception as err:
            self.update(error="采集未完成，可再次更新。错误类型 " + type(err).__name__)
        finally:
            with self.lock:
                self.last_done = time.monotonic()
                self.state.update(running=False, finished=datetime.now(timezone.utc).isoformat())


class QuotaRefresher:
    def __init__(self, database):
        self.file = database
        self.lock = threading.Lock()
        self.running = False
        self.last_done = 0.0

    def snapshot(self):
        with self.lock:
            return {"running": self.running}

    def start(self, force=False):
        with self.lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self.run, args=(force,), daemon=True).start()
        return True

    def maybe_start(self, min_gap=30.0):
        with self.lock:
            if self.running or (self.last_done and time.monotonic() - self.last_done < min_gap):
                return False
            self.running = True
        threading.Thread(target=self.run, args=(False,), daemon=True).start()
        return True

    def run(self, force=False):
        try:
            snap_dir = self.file.parent / "quota-snapshots"
            agy_dir = self.file.parent / "agy-quota"
            with closing(connect(self.file)) as db:
                try:
                    quota.refresh(db, force=force)
                except (ValueError, OSError, sqlite3.Error):
                    pass
                if not codex_search.setting(db, "codex_only", False):
                    try:
                        agy_quota.snapshot(db, agy_dir, refresh=True, force=force)
                    except (ValueError, OSError, sqlite3.Error):
                        pass
                    for key, info in subscriptions.catalogue(db, snap_dir).items():
                        if info.get("automatic"):
                            try:
                                subscriptions.handle(db, key, snap_dir, write=True, force=force)
                            except (ValueError, OSError, sqlite3.Error):
                                pass
        except (ValueError, OSError, sqlite3.Error):
            pass
        finally:
            with self.lock:
                self.running = False
                self.last_done = time.monotonic()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def headers_for(self, status, mime, size=None, filename=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; connect-src 'self'",
        )
        if size is not None:
            self.send_header("Content-Length", str(size))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        self.headers_for(status, "application/json; charset=utf-8", len(raw))
        self.wfile.write(raw)

    def export(self, db, args):
        fmt = args.get("format", "csv")
        if fmt not in ("csv", "json"):
            raise ValueError("请选择 CSV 或 JSON")
        _, _, zone, _, _ = query.scope(args)
        db.execute("BEGIN")
        rows = query.records(db, args)
        today = datetime.now(zone).strftime("%Y%m%d")
        self.headers_for(
            200,
            ("text/csv" if fmt == "csv" else "application/json") + "; charset=utf-8",
            filename=f"meter-usage-{today}.{fmt}",
        )
        if fmt == "json":
            meta = {"schema": "meter.export.v1", "timezone": args.get("tz", "UTC"), "filters": args}
            self.wfile.write((json.dumps(meta, ensure_ascii=False)[:-1] + ',"records":[').encode())
            for i, row in enumerate(rows):
                self.wfile.write(
                    (
                        ("," if i else "")
                        + json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                    ).encode()
                )
            self.wfile.write(b"]}")
        else:
            keys = [
                "timestamp",
                "local_time",
                "timezone",
                "platform",
                "entry",
                "entry_name",
                "vendor",
                "model",
                "project",
                *FIELDS,
                "net",
                "source",
                "uid","account","task","raw_project",
            ]
            buf = io.StringIO(newline="")
            writer = csv.DictWriter(buf, fieldnames=keys)
            writer.writeheader()
            self.wfile.write(b"\xef\xbb\xbf" + buf.getvalue().encode())
            for row in rows:
                row["timezone"] = args.get("tz", "UTC")
                # Keep spreadsheet applications from executing a project or model label.
                for k, v in row.items():
                    if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")):
                        row[k] = "'" + v
                buf.seek(0)
                buf.truncate()
                writer.writerow(row)
                self.wfile.write(buf.getvalue().encode())

    def handle_request(self, write):
        host = self.headers.get("Host")
        if host != f"127.0.0.1:{self.server.server_port}":
            return self.send_json({"error": "仅可从本机地址打开"}, 403)
        url = urlsplit(self.path)
        args = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path
        if not write and path in STATIC:
            name, mime = STATIC[path]
            raw = (ROOT / "web" / name).read_bytes()
            self.headers_for(200, mime + "; charset=utf-8", len(raw))
            return self.wfile.write(raw)
        if write:
            if (
                self.headers.get("Origin") not in (None, "http://" + host)
                or self.headers.get("X-Ledger-Local") != "1"
            ):
                return self.send_json({"error": "请从本地账本页面操作"}, 403)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 32 * 1024**2:
                return self.send_json({"error": "单个导入文件上限为 32 MiB，请按请求拆分"}, 413)
            body = self.rfile.read(length)
            if path == "/api/sync":
                self.server.job.start()
                return self.send_json(self.server.job.snapshot())
            if path == "/api/widget/refresh":
                if args.get("period") == "quota":
                    qjob = getattr(self.server, "quota_job", None)
                    if qjob is not None:
                        qjob.start(force=True)
                        return self.send_json({"ok": True, "syncing": qjob.snapshot()["running"]})
                    return self.send_json({"ok": True, "syncing": False})
                self.server.job.start()
                return self.send_json({"ok": True, "syncing": self.server.job.snapshot()["running"]})
            if path == "/api/stop":
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
        if not write and path == "/api/instance":
            return self.send_json({"app": "meter", "version": VERSION, "build": BUILD, "instance": self.server.identity})
        with closing(connect(self.server.job.file)) as db:
            if path == "/api/appearance":
                prefs = appearance.write(db, json.loads(body)) if write else appearance.read(db)
                return self.send_json(dict(settings=prefs,presets=appearance.PRESETS,fonts=appearance.FONTS))
            if path == "/api/pricing":
                return self.send_json(pricing.write_override(db, json.loads(body)) if write else pricing.catalogue_payload(db))
            if path == "/api/widget/open" and write:
                zone = args.get("tz")
                if zone:
                    query.scope({"tz":zone,"period":"today"})
                open_widget(self.server.server_port, self.server.job.file, zone)
                return self.send_json({"ok":True})
            if path == "/api/widget" and not write:
                period = args.get("period", "today")
                data = {"appearance": appearance.read(db)}
                if period == "quota":
                    qjob = getattr(self.server, "quota_job", None)
                    if getattr(self.server, "auto_sync", False) and qjob is not None:
                        qjob.maybe_start(min_gap=30.0)
                    snapshots = [("Codex", quota.cached(db))]
                    if not codex_search.setting(db, "codex_only", False):
                        snapshots.append(("Antigravity", codex_search.setting(db, "agy_quota_snapshot")))
                        snapshots.extend(
                            (s[0], codex_search.setting(db, "subscription:" + k))
                            for k, s in subscriptions.SERVICES.items()
                        )
                    data["quotas"] = [
                        {"name": name, "at": snap["at"], "windows": snap["windows"]}
                        for name, snap in snapshots
                        if snap
                    ]
                    data["syncing"] = qjob.snapshot()["running"] if qjob is not None else False
                else:
                    if getattr(self.server, "auto_sync", False):
                        self.server.job.maybe_start(min_gap=8.0)
                    view = query.view(db, {"period": period, "tz": args.get("tz", "UTC")})
                    data.update(
                        totals=view["totals"],
                        top=next((r["model"] for r in view["rank"] if r["total"] is not None), None),
                        scope=view["scope"],
                        value=view["totals"].get("value"),
                        value_n=view["totals"].get("value_n", 0),
                        revision=query.revision(db),
                        syncing=self.server.job.snapshot()["running"],
                    )
                return self.send_json(data)
            if path.startswith("/api/subscription/"):
                key = path.removeprefix("/api/subscription/")
                imported = None
                if write and args.get("import") == "1":
                    if len(body) > 1024 * 1024:
                        raise ValueError("额度 JSON 上限 1 MiB")
                    imported = json.loads(body)
                    if not isinstance(imported, dict):
                        raise ValueError("请选择额度 JSON 对象")
                return self.send_json(subscriptions.handle(db, key, self.server.job.file.parent / "quota-snapshots", write=write,
                    force=args.get("force") == "1", imported=imported))
            if path == "/api/codex-quota":
                return self.send_json(quota.refresh(db, force=args.get("auto") != "1") if write else quota.cached(db))
            if path == "/api/agy-quota":
                return self.send_json(agy_quota.snapshot(db, self.server.job.file.parent / "agy-quota",
                    refresh=write, force=write and args.get("force") == "1"))
            if path == "/api/batch" and not write:
                return self.send_json(evidence.batch_rows(db,int(args["id"]),int(args.get("page",0))))
            if path == "/api/manage" and not write:
                return self.send_json(evidence.management(db,int(args.get("page",0))))
            if path == "/api/evidence" and not write:
                return self.send_json(evidence.review(db,args.get("uid","")))
            if write:
                if path == "/api/correct":
                    return self.send_json(evidence.correct(db,json.loads(body)))
                if path == "/api/undo":
                    return self.send_json(undo(db,json.loads(body)["batch"]))
                if path == "/api/codex-search":
                    codex_search.set_backup(db, json.loads(body)["backup"])
                    return self.send_json(codex_search.status(db))
                if path == "/api/import":
                    return self.send_json(import_file(db, body, args.get("format"), preview=args.get("preview")=="1"))
                if path == "/api/notes":
                    return self.send_json(manual.save(db, json.loads(body)))
                if path == "/api/notes/remove":
                    with db:
                        db.execute("DELETE FROM notes WHERE id=?", (json.loads(body)["id"],))
                    return self.send_json({"ok": True})
            elif path == "/api/status":
                return self.send_json(dict(self.server.job.snapshot(), revision=query.revision(db), alerts=evidence.alerts(db)))
            elif path == "/api/view":
                return self.send_json(query.view(db, args))
            elif path == "/api/options":
                return self.send_json(
                    {"values": query.options(db), "entries": {k: v[0] for k, v in ENTRIES.items()},
                     "subscriptions": subscriptions.catalogue(db, self.server.job.file.parent / "quota-snapshots"),
                     "codex_profile": codex_search.current_root(db).name if codex_search.setting(db, "codex_only", False) else None}
                )
            elif path == "/api/sources":
                return self.send_json(query.sources(db))
            elif path == "/api/codex-search":
                return self.send_json(codex_search.status(db))
            elif path == "/api/notes":
                return self.send_json(manual.list_notes(db,args))
            elif path == "/api/export":
                return self.export(db, args)
        self.send_json({"error": "未找到此资源"}, 404)

    def guarded(self, write):
        try:
            self.handle_request(write)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except (ValueError, KeyError, TypeError, UnicodeError, OverflowError) as err:
            self.send_json(
                {"error": str(err) if isinstance(err, ValueError) else "字段类型或格式不符"}, 400
            )
        except sqlite3.Error:
            self.send_json({"error": "本地数据库暂不可用，请稍后重试"}, 503)

    def do_GET(self):
        self.guarded(False)

    def do_POST(self):
        self.guarded(True)


class LocalServer(ThreadingHTTPServer):
    def server_bind(self):
        # Windows SO_REUSEADDR can let two ledgers listen on the same port.
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def local_request(port, path, *, stop=False):
    conn = HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("POST" if stop else "GET", path, body=b"" if stop else None,
                     headers={"X-Ledger-Local": "1"} if stop else {})
        response = conn.getresponse()
        return json.loads(response.read(65536)) if response.status == 200 else {}
    finally:
        conn.close()


def prepare_database(file, profile):
    initialize(file)
    if profile:
        with closing(connect(file)) as db, db:
            codex_search.save(db, "home", str(profile.resolve()))
            codex_search.save(db, "codex_only", True)


def serve(args, file):
    identity = digest("meter-install", str(file.resolve()))
    for port in range(args.port, args.port + 16):
        try:
            server = LocalServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            try:
                reply = local_request(port, "/api/instance")
            except (ValueError, OSError, HTTPException):
                continue
            if not isinstance(reply, dict) or reply.get("app") != "meter" or reply.get("instance") != identity:
                continue
            if reply.get("version") == VERSION and reply.get("build") == BUILD:
                if args.widget:
                    open_widget(port, file)
                elif not args.no_browser:
                    webbrowser.open(f"http://127.0.0.1:{port}/")
                print(f"账本已运行 http://127.0.0.1:{port}/")
                return
            hint = "同一账本有其他版本正在运行。请关闭旧服务窗口，再重新启动。"
            if reply.get("version") not in ("1.6", VERSION):
                raise SystemExit(hint)
            try:
                if local_request(port, "/api/status").get("running") is not False:
                    raise SystemExit("旧版仍在采集。请等它结束后重新启动新版。")
                if not local_request(port, "/api/stop", stop=True).get("ok"):
                    raise SystemExit(hint)
            except (OSError, ValueError, AttributeError, HTTPException):
                raise SystemExit(hint) from None
            for _ in range(50):
                time.sleep(0.1)
                try:
                    server = LocalServer(("127.0.0.1", port), Handler)
                    break
                except OSError:
                    continue
            else:
                raise SystemExit(hint)
            break
    else:
        raise SystemExit("本机端口均被占用，可使用 --port 选择其他端口。")
    try:
        prepare_database(file, args.codex_home)
    except BaseException:
        server.server_close()
        raise
    server.identity, server.job = identity, Collector(file)
    server.quota_job, server.auto_sync = QuotaRefresher(file), not args.no_sync
    if not args.no_sync:

        def periodic():
            while True:
                server.job.start()
                time.sleep(30)

        threading.Thread(target=periodic, daemon=True).start()
    link = f"http://127.0.0.1:{server.server_port}/"
    print(f"AI 用量账本  {link}\n关闭本窗口或按 Ctrl+C 即可退出。", flush=True)
    if args.widget:
        open_widget(server.server_port, file)
    elif not args.no_browser:
        webbrowser.open(link)
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI 用量账本")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--codex-home", type=Path, help="为指定 Codex 配置目录打开独立账本，仅采集该目录的 Codex 用量")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--widget", action="store_true", help="打开桌面小组件")
    parser.add_argument("--no-sync", action="store_true", help="仅浏览数据库，暂停后台采集")
    parser.add_argument("--collect", action="store_true", help="更新统计后退出")
    args = parser.parse_args()
    directory = args.data_dir or ROOT / "private"
    if args.codex_home:
        profile = args.codex_home.resolve()
        if not profile.is_dir():
            parser.error("Codex 配置目录不存在")
        directory = directory / "codex-profiles" / digest("codex-home", str(profile).casefold())
    file = directory / "ledger.sqlite3"
    if args.collect:
        prepare_database(file, args.codex_home)
        print(json.dumps(sync(file), ensure_ascii=False))
    else:
        serve(args, file)
