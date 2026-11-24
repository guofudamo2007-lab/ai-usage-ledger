import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import ledger_app as app


class StartupTest(unittest.TestCase):
    def exercise(self, version, running, expect_stop):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "ledger.sqlite3"
            args = SimpleNamespace(port=19000, no_sync=True, no_browser=True, widget=False, codex_home=None)
            replies = [
                dict(app="meter", instance=app.digest("meter-install", str(file.resolve())), version=version),
                dict(running=running), dict(ok=True),
            ]
            server = MagicMock(server_port=args.port)
            with patch.object(app, "local_request", side_effect=replies) as request, patch.object(
                app, "LocalServer", side_effect=[OSError(), server]
            ):
                if running and version != app.VERSION:
                    with self.assertRaises(SystemExit):
                        app.serve(args, file)
                else:
                    app.serve(args, file)
                self.assertEqual(file.exists(), expect_stop)
                self.assertEqual(any(c.kwargs.get("stop") for c in request.call_args_list), expect_stop)
                self.assertEqual(server.serve_forever.called, expect_stop)

    def test_idle_legacy_service_hands_over_before_migration(self):
        self.exercise("1.6", False, True)

    def test_active_legacy_scan_keeps_original_database(self):
        self.exercise("1.6", True, False)

    def test_current_service_is_reused_without_migration(self):
        self.exercise(app.VERSION, True, False)
