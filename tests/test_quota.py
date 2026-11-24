import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ledger import quota
from ledger.codex_search import save


class QuotaTests(unittest.TestCase):
    def test_remaining_and_partial_windows(self):
        for used, remaining in ((0, 100), (100, 0), (31.5, 68.5)):
            bucket = {"primary": {"usedPercent": used, "windowDurationMins": 10080},
                      "secondary": {"resetsAt": 42}}
            self.assertEqual(quota.windows(bucket), [
                {"slot": "primary", "remaining": remaining, "minutes": 10080, "reset": None}])
        for used in (None, True, -1, 101, float("nan")):
            self.assertEqual(quota.windows({"primary": {"usedPercent": used}}), [])
        self.assertEqual(quota.windows({"limitId": "other", "primary": {"usedPercent": 0}}), [])

    def test_failed_refresh_preserves_only_matching_profile_snapshot(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("CREATE TABLE codex_search_settings(key TEXT PRIMARY KEY,value TEXT)")
            with db:
                save(db, "home", str(Path.cwd() / "fictional-profile-a"))
            self.assertIsNone(quota.cached(db))
            snapshot = {"at": "2025-01-01T00:00:00+00:00", "windows": [], "source": "rpc"}
            with patch.object(quota, "read_live", return_value=snapshot):
                self.assertEqual(quota.refresh(db), snapshot)
            with patch.object(quota, "read_live", side_effect=ValueError("offline")):
                with self.assertRaises(ValueError):
                    quota.refresh(db)
            self.assertEqual(quota.cached(db), snapshot)
            with db:
                save(db, "home", str(Path.cwd() / "fictional-profile-b"))
            self.assertIsNone(quota.cached(db))
