"""Windows local usage ledger. Standard library only, no outbound data connections."""

import argparse
import csv
import io
import json
import sqlite3
import threading
import time
import webbrowser
from contextlib import closing
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ledger import manual, query
from ledger.collect import sync
from ledger.core import ENTRIES, FIELDS, connect, digest, initialize
from ledger.imports import import_file

ROOT = Path(__file__).resolve().parent
PORT = 17863
STATIC = {
    "/": ("index.html", "text/html"),
    "/style.css": ("style.css", "text/css"),
    "/app.js": ("app.js", "text/javascript"),
    "/mark.svg": ("mark.svg", "image/svg+xml"),
    "/fonts/IBMPlexMono-Medium.ttf": ("fonts/IBMPlexMono-Medium.ttf", "font/ttf"),
}
for asset, mime in (("uplot.js", "text/javascript"), ("tom-select.js", "text/javascript"), ("components.css", "text/css"), ("icons.svg", "image/svg+xml")):
    STATIC[f"/vendor/{asset}"] = (f"vendor/{asset}", mime)
for module in ("ui", "books", "instruments"):
    STATIC[f"/{module}.js"] = (f"{module}.js", "text/javascript")


class Collector:
    def __init__(self, database):
        self.file = database
        self.lock = threading.Lock()
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

    def run(self):
        try:
            self.update(report=sync(self.file, self.update))
        except Exception as err:
            self.update(error="采集未完成，可再次更新。错误类型 " + type(err).__name__)
        finally:
            self.update(running=False, finished=datetime.now(timezone.utc).isoformat())


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
                "uid",
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
            if path == "/api/stop":
                self.send_json({"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
        if not write and path == "/api/instance":
            return self.send_json({"app": "meter", "version": "1.2", "instance": self.server.identity})
        with closing(connect(self.server.job.file)) as db:
            if write:
                if path == "/api/import":
                    return self.send_json(import_file(db, body, args.get("format")))
                if path == "/api/notes":
                    return self.send_json(manual.save(db, json.loads(body)))
                if path == "/api/notes/remove":
                    with db:
                        db.execute("DELETE FROM notes WHERE id=?", (json.loads(body)["id"],))
                    return self.send_json({"ok": True})
            elif path == "/api/status":
                return self.send_json(dict(self.server.job.snapshot(), revision=query.revision(db)))
            elif path == "/api/view":
                return self.send_json(query.view(db, args))
            elif path == "/api/options":
                return self.send_json(
                    {"values": query.options(db), "entries": {k: v[0] for k, v in ENTRIES.items()}}
                )
            elif path == "/api/sources":
                return self.send_json(query.sources(db))
            elif path == "/api/notes":
                return self.send_json(manual.list_notes(db))
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


def serve(args, file):
    identity = digest("meter-install", str(file.resolve()))
    for port in range(args.port, args.port + 16):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            conn = HTTPConnection("127.0.0.1", port, timeout=0.8)
            try:
                conn.request("GET", "/api/instance")
                reply = json.loads(conn.getresponse().read(512))
                if reply.get("instance") == identity:
                    if not args.no_browser:
                        webbrowser.open(f"http://127.0.0.1:{port}/")
                    print(f"账本已运行 http://127.0.0.1:{port}/")
                    return
            except (ValueError, OSError, AttributeError, HTTPException):
                pass
            finally:
                conn.close()
    else:
        raise SystemExit("本机端口均被占用，可使用 --port 选择其他端口。")
    server.identity, server.job = identity, Collector(file)
    if not args.no_sync:

        def periodic():
            while True:
                server.job.start()
                time.sleep(300)

        threading.Thread(target=periodic, daemon=True).start()
    link = f"http://127.0.0.1:{server.server_port}/"
    print(f"AI 用量账本  {link}\n关闭本窗口或按 Ctrl+C 即可退出。", flush=True)
    if not args.no_browser:
        webbrowser.open(link)
    with server:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI 用量账本")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "private")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-sync", action="store_true", help="仅浏览数据库，暂停后台采集")
    parser.add_argument("--collect", action="store_true", help="更新统计后退出")
    args = parser.parse_args()
    file = args.data_dir / "ledger.sqlite3"
    initialize(file)
    if args.collect:
        print(json.dumps(sync(file), ensure_ascii=False))
    else:
        serve(args, file)
