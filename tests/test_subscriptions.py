import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ledger import manual, subscriptions as sub
from ledger.codex_search import save
from ledger.core import connect, initialize


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name)
        file = self.path / "ledger.sqlite3"
        initialize(file)
        self.db = connect(file)
        self.addCleanup(self.db.close)

    def test_missing_zero_and_unlimited_are_distinct(self):
        rows = sub.copilot_windows({"quota_reset_date_utc": "2030-01-01T00:00:00Z",
            "quota_snapshots": {"chat": {"has_quota": True, "percent_remaining": 0},
                "completions": {"unlimited": True}, "premium_interactions": {"has_quota": False, "percent_remaining": 0},
                "unknown": {}}})
        self.assertEqual([w["remaining"] for w in rows], [0, None])
        self.assertEqual(rows[1]["state"], "不限量")
        self.assertEqual(rows[0]["reset"], 1893456000)
        data = {"model_name": "Example", "current_interval_usage_count": 20, "current_interval_total_count": 50}
        self.assertFalse(sub.minimax_windows({"model_remains": [data]}))
        data.update(current_interval_remaining_percent=40, current_weekly_status=3)
        self.assertEqual([w["remaining"] for w in sub.minimax_windows({"model_remains": [data]})], [40,None])
        data.update(current_weekly_status=1,current_weekly_remaining_percent=80,weekly_boost_permille=1500)
        self.assertEqual(sub.minimax_windows({"model_remains":[data]})[1]["remaining"],120)

    def test_grok_reads_own_observation_and_skips_unrelated_logs(self):
        p = self.path / "unified.jsonl"
        record = {"ts":"2025-01-01T00:00:00Z","msg":"billing: fetched credits config",
                  "ctx":{"config":{"creditUsagePercent":0,"currentPeriod":{"type":"USAGE_PERIOD_TYPE_WEEKLY",
                        "end":"2025-01-08T00:00:00Z"}}}}
        p.write_bytes(json.dumps(record).encode() + b"\n" + b"x"*135000 + b"\n")
        with patch.object(sub,"grok_log",return_value=p):
            fresh = sub.handle(self.db,"grok",self.path,write=True)
            self.assertEqual(fresh["windows"][0]["remaining"],100)
            self.assertEqual(fresh["at"],"2025-01-01T00:00:00+00:00")
            with self.assertRaises(ValueError):
                p.unlink()
                sub.handle(self.db,"grok",self.path,write=True,force=True)
            self.assertEqual(sub.handle(self.db,"grok",self.path),fresh)

    def test_import_watch_and_account_scope(self):
        claude = {"rate_limits":{"five_hour":{"used_percentage":50}}, "ignored":"fictional-private-field"}
        snapshot = sub.handle(self.db,"claude-code",self.path,write=True,imported=claude)
        self.assertEqual(snapshot["source"],"import")
        self.assertNotIn("fictional-private-field",str(snapshot))
        (self.path/"claude-code.json").write_text(json.dumps(claude))
        watched = sub.handle(self.db,"claude-code",self.path,write=True)
        self.assertEqual(watched["source"],"file")
        self.assertEqual(watched, sub.handle(self.db,"claude-code",self.path,write=True))
        kimi = {"data":{"kind":"ok","quota":{"usages":{"limit5h":{"usedRatio":0.25}}}}}
        self.assertEqual(sub.imported_windows("kimi-code",kimi)[0]["remaining"],75)
        with self.db:
            save(self.db,"codex_only",True)
        self.assertEqual(sub.catalogue(self.db,self.path),{})
        with self.assertRaises(ValueError):
            sub.handle(self.db,"kimi-code",self.path,write=True,imported=kimi)

    def test_manual_subscription_does_not_create_usage(self):
        manual.save(self.db, {"entry":"midjourney","kind":"quota","day":"2025-01-01","amount":"0","unit":"小时"})
        self.assertEqual(manual.list_notes(self.db)[0]["entry"],"midjourney")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0],0)
