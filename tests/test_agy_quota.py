import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ledger import agy_quota
from ledger.codex_search import save


class AgyQuotaTests(unittest.TestCase):
    def report(self, text, turns=0):
        return {"status": "SUCCESS", "num_turns": turns, "response": text}

    def test_shared_group_and_missing_reset(self):
        response = self.report("Example group\tWeekly Limit Remaining\t0%\t2030-01-01T00:00:00Z\n"
                               "Example group\tFive Hour Limit Remaining\t100%\t\n")
        windows = agy_quota.parse_report(response)
        self.assertEqual([row["group"] for row in windows], ["Example group"] * 2)
        self.assertEqual([row["remaining"] for row in windows], [0, 100])
        self.assertEqual([row["reset"] for row in windows], [1893456000, None])

    def test_reject_unverified_values_and_model_responses(self):
        for text in ("", "a\tb\tunknown", "a\tb\t130%", "a\tb\t30%\t2030-01-01"):
            with self.assertRaises(ValueError):
                agy_quota.parse_report(self.report(text))
        with self.assertRaises(ValueError):
            agy_quota.parse_report(self.report("a\tb\t15%", turns=1))

    def test_auto_refresh_cache_and_profile_isolation(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE codex_search_settings(key TEXT PRIMARY KEY,value TEXT)")
        data = {"at": datetime.now(timezone.utc).isoformat(), "windows": [{"remaining": 0}]}
        with patch.object(agy_quota, "read_live", return_value=data) as read:
            self.assertIsNone(agy_quota.snapshot(db, Path(".")))
            for _ in range(2):
                self.assertEqual(agy_quota.snapshot(db, Path("."), refresh=True), data)
            read.assert_called_once()
            read.side_effect = ValueError("offline")
            with self.assertRaises(ValueError):
                agy_quota.snapshot(db, Path("."), refresh=True, force=True)
            self.assertEqual(agy_quota.snapshot(db, Path(".")), data)
            with db:
                save(db, "codex_only", True)
            with self.assertRaises(ValueError):
                agy_quota.snapshot(db, Path("."), refresh=True, force=True)
            self.assertEqual(read.call_count, 2)
