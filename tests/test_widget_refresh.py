import json
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import ledger_app as app
from ledger.core import initialize


class WidgetRefreshTests(unittest.TestCase):
    def test_collector_maybe_start_throttles_and_dedups(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ledger.sqlite3"
            initialize(db_path)
            started = threading.Event()
            release = threading.Event()

            def fake_sync(file, progress):
                started.set()
                release.wait(2.0)
                return {"added": 0}

            collector = app.Collector(db_path)
            with patch.object(app, "sync", side_effect=fake_sync) as mock_sync:
                self.assertTrue(collector.maybe_start(min_gap=8.0))
                self.assertTrue(started.wait(1.0))
                self.assertFalse(collector.maybe_start(min_gap=0.0))
                release.set()
                for _ in range(50):
                    if not collector.snapshot()["running"]:
                        break
                    time.sleep(0.02)
                self.assertFalse(collector.snapshot()["running"])
                self.assertFalse(collector.maybe_start(min_gap=8.0))
                self.assertEqual(mock_sync.call_count, 1)
                collector.last_done = time.monotonic() - 10.0
                self.assertTrue(collector.maybe_start(min_gap=8.0))
                for _ in range(50):
                    if not collector.snapshot()["running"]:
                        break
                    time.sleep(0.02)
                self.assertEqual(mock_sync.call_count, 2)

    def test_quota_refresher_best_effort_per_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ledger.sqlite3"
            initialize(db_path)
            refresher = app.QuotaRefresher(db_path)
            with patch.object(app.quota, "refresh", side_effect=ValueError("codex missing")), patch.object(
                app.agy_quota, "snapshot"
            ) as mock_agy, patch.object(
                app.subscriptions,
                "catalogue",
                return_value={"copilot": {"automatic": True}, "claude-code": {"automatic": False}},
            ), patch.object(
                app.subscriptions, "handle"
            ) as mock_sub:
                refresher.run(force=True)
                mock_agy.assert_called_once()
                self.assertTrue(mock_agy.call_args.kwargs["refresh"])
                self.assertTrue(mock_agy.call_args.kwargs["force"])
                mock_sub.assert_called_once()
                self.assertEqual(mock_sub.call_args.args[1], "copilot")
                self.assertTrue(mock_sub.call_args.kwargs["force"])

    def _serve_isolated(self, db_path, *, auto_sync):
        server = app.LocalServer(("127.0.0.1", 0), app.Handler)
        server.identity = app.digest("meter-test", str(db_path))
        server.job = MagicMock(file=db_path)
        server.job.snapshot.return_value = {"running": False}
        server.job.maybe_start.return_value = auto_sync
        server.quota_job = MagicMock()
        server.quota_job.snapshot.return_value = {"running": False}
        server.quota_job.maybe_start.return_value = auto_sync
        server.auto_sync = auto_sync
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_widget_endpoints_and_refresh_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ledger.sqlite3"
            initialize(db_path)

            # With auto_sync=False (--no-sync), GET /api/widget must not schedule background scans
            srv_no, t_no = self._serve_isolated(db_path, auto_sync=False)
            try:
                conn = HTTPConnection("127.0.0.1", srv_no.server_port, timeout=3)
                conn.request("GET", "/api/widget?period=today&tz=Asia%2FShanghai")
                res = conn.getresponse()
                self.assertEqual(res.status, 200)
                data = json.loads(res.read())
                self.assertIn("revision", data)
                self.assertIs(data["syncing"], False)
                srv_no.job.maybe_start.assert_not_called()

                conn.request("GET", "/api/widget?period=quota&tz=Asia%2FShanghai")
                res = conn.getresponse()
                self.assertEqual(res.status, 200)
                qdata = json.loads(res.read())
                self.assertEqual(qdata["quotas"], [])
                self.assertIs(qdata["syncing"], False)
                srv_no.quota_job.maybe_start.assert_not_called()
                conn.close()
            finally:
                srv_no.shutdown()
                srv_no.server_close()
                t_no.join(timeout=2)

            # With auto_sync=True and POST /api/widget/refresh
            srv, t = self._serve_isolated(db_path, auto_sync=True)
            try:
                conn = HTTPConnection("127.0.0.1", srv.server_port, timeout=3)
                conn.request("GET", "/api/widget?period=today&tz=Asia%2FShanghai")
                res = conn.getresponse()
                self.assertEqual(res.status, 200)
                res.read()
                srv.job.maybe_start.assert_called_once_with(min_gap=8.0)

                conn.request("GET", "/api/widget?period=quota&tz=Asia%2FShanghai")
                res = conn.getresponse()
                self.assertEqual(res.status, 200)
                res.read()
                srv.quota_job.maybe_start.assert_called_once_with(min_gap=30.0)

                # POST without X-Ledger-Local is rejected
                conn.request("POST", "/api/widget/refresh?period=today", body=b"")
                res = conn.getresponse()
                self.assertEqual(res.status, 403)
                res.read()

                # POST with X-Ledger-Local=1 forces sync
                conn.request(
                    "POST",
                    "/api/widget/refresh?period=today",
                    body=b"",
                    headers={"X-Ledger-Local": "1"},
                )
                res = conn.getresponse()
                self.assertEqual(res.status, 200)
                reply = json.loads(res.read())
                self.assertTrue(reply["ok"])
                srv.job.start.assert_called_once()

                conn.request(
                    "POST",
                    "/api/widget/refresh?period=quota",
                    body=b"",
                    headers={"X-Ledger-Local": "1"},
                )
                res = conn.getresponse()
                self.assertEqual(res.status, 200)
                reply = json.loads(res.read())
                self.assertTrue(reply["ok"])
                srv.quota_job.start.assert_called_once_with(force=True)
                conn.close()
            finally:
                srv.shutdown()
                srv.server_close()
                t.join(timeout=2)

    def test_stale_build_service_hands_over(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "ledger.sqlite3"
            args = SimpleNamespace(port=19001, no_sync=True, no_browser=True, widget=False, codex_home=None)
            replies = [
                dict(app="meter", instance=app.digest("meter-install", str(file.resolve())), version=app.VERSION, build="stale-build"),
                dict(running=False),
                dict(ok=True),
            ]
            server = MagicMock(server_port=args.port)
            with patch.object(app, "local_request", side_effect=replies) as request, patch.object(
                app, "LocalServer", side_effect=[OSError(), server]
            ):
                app.serve(args, file)
                self.assertTrue(file.exists())
                self.assertTrue(any(c.kwargs.get("stop") for c in request.call_args_list))
                self.assertTrue(server.serve_forever.called)
