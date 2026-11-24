import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ledger import pricing, query
from ledger.core import connect, counters, event, ingest, initialize, stamp


class PricingTests(unittest.TestCase):
    def test_builtin_matcher_and_unknown_guard(self):
        self.assertEqual(pricing.resolve("claude-opus-4-6")["input"], 5.0)
        self.assertEqual(pricing.resolve("claude-opus-4-6")["output"], 25.0)
        self.assertEqual(pricing.resolve("claude-sonnet-4-5-20250929")["input"], 3.0)
        self.assertEqual(pricing.resolve("claude-opus-4-20250514")["input"], 15.0)
        self.assertEqual(pricing.resolve("gpt-5.3-codex")["input"], 1.75)
        self.assertEqual(pricing.resolve("gpt-5.3-codex")["output"], 14.0)
        self.assertEqual(pricing.resolve("gpt-5-mini")["input"], 0.25)
        self.assertEqual(pricing.resolve("models/gemini-3.1-pro-preview")["input"], 2.0)
        self.assertEqual(pricing.resolve("gemini-2.5-flash")["output"], 2.5)
        self.assertEqual(pricing.resolve("grok-4.6")["output"], 6.0)
        self.assertEqual(pricing.resolve("kimi-k2.5")["input"], 0.60)
        # Unknown or placeholder model names must never receive fabricated prices.
        for unknown in ("gpt-fictional", "AGY #42 · 型号未确认", "型号未记录", "", None):
            self.assertIsNone(pricing.resolve(unknown))

    def test_cost_parts_and_cache_savings(self):
        rate = pricing.resolve("claude-sonnet-4-6")
        # 200k fresh input, 1M cache read, 100k cache write, 50k output
        total, v_in, v_cr, v_cw, v_out, no_cache, saved = pricing.cost_parts(
            200_000, 1_000_000, 100_000, 50_000, rate
        )
        # in: 0.2 * 3 = 0.60; cr: 1 * 0.3 = 0.30; cw: 0.1 * 3.75 = 0.375; out: 0.05 * 15 = 0.75
        self.assertAlmostEqual(v_in, 0.60, places=6)
        self.assertAlmostEqual(v_cr, 0.30, places=6)
        self.assertAlmostEqual(v_cw, 0.375, places=6)
        self.assertAlmostEqual(v_out, 0.75, places=6)
        self.assertAlmostEqual(total, 2.025, places=6)
        # Without cache: 1.3M * 3 + 0.05M * 15 = 3.9 + 0.75 = 4.65
        self.assertAlmostEqual(no_cache, 4.65, places=6)
        self.assertAlmostEqual(saved, 2.625, places=6)

    def test_view_valuation_partial_coverage_and_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ledger.sqlite3"
            initialize(db_path)
            ts = stamp("2026-11-23T12:00:00+08:00")
            rows = [
                # 1M total: 900k input (800k cache_read, 100k fresh), 100k output on claude-sonnet-4-6
                # Expected USD: fresh 0.1*3=0.3 + cr 0.8*0.3=0.24 + out 0.1*15=1.50 = 2.04
                event(
                    "u1",
                    ts,
                    "claude-api",
                    "Anthropic",
                    "claude-sonnet-4-6",
                    "演示",
                    "test",
                    counters(total=1_000_000, input=900_000, output=100_000, cache_read=800_000),
                ),
                # 1M total only (missing input/output) -> must NOT be priced!
                event(
                    "u2",
                    ts + 1000,
                    "claude-api",
                    "Anthropic",
                    "claude-sonnet-4-6",
                    "演示",
                    "test",
                    counters(total=1_000_000),
                ),
                # Unpriced custom model
                event(
                    "u3",
                    ts + 2000,
                    "openai-api",
                    "OpenAI",
                    "gpt-fictional",
                    "演示",
                    "test",
                    counters(total=1_000_000, input=800_000, output=200_000),
                ),
            ]
            with closing(connect(db_path)) as db:
                with db:
                    ingest(db, rows)
                res = query.view(db, {"period": "all", "tz": "Asia/Shanghai", "sort": "value", "rankings": "1"})
                p = res["pricing"]
                self.assertEqual(p["priced_n"], 1)
                self.assertEqual(p["total_n"], 3)
                self.assertAlmostEqual(p["total"], 2.04, places=5)
                self.assertAlmostEqual(p["saved"], 2.16, places=5)
                self.assertEqual(res["rankings"]["model"][0]["model"], "claude-sonnet-4-6")
                self.assertIsNone(res["rankings"]["model"][1]["value"])

                # Add custom override for gpt-fictional ($10/M input, $20/M output)
                pricing.write_override(
                    db,
                    {
                        "model": "gpt-fictional",
                        "input": 10.0,
                        "cache_read": 1.0,
                        "cache_write": 10.0,
                        "output": 20.0,
                    },
                )
                res2 = query.view(db, {"period": "all", "tz": "Asia/Shanghai", "sort": "value", "rankings": "1"})
                # gpt-fictional: 0.8*10 + 0.2*20 = $12.00 -> should now rank #1!
                self.assertEqual(res2["rankings"]["model"][0]["model"], "gpt-fictional")
                self.assertAlmostEqual(res2["pricing"]["total"], 14.04, places=5)
                self.assertEqual(res2["pricing"]["priced_n"], 2)

                # Reset override
                pricing.write_override(db, {"model": "gpt-fictional", "reset": True})
                res3 = query.view(db, {"period": "all", "tz": "Asia/Shanghai", "sort": "value"})
                self.assertAlmostEqual(res3["pricing"]["total"], 2.04, places=5)
                self.assertEqual(res3["pricing"]["priced_n"], 1)

                # Validation guards
                with self.assertRaises(ValueError):
                    pricing.write_override(db, {"model": "x", "input": -1, "cache_read": 0, "cache_write": 0, "output": 0})
                with self.assertRaises(ValueError):
                    pricing.write_override(db, {"model": "", "input": 1, "cache_read": 0, "cache_write": 0, "output": 0})

                # Default model drill-down (detail=1) on a priced model must not crash or leak COST_PARTS
                drill = query.view(
                    db,
                    {
                        "period": "all",
                        "tz": "Asia/Shanghai",
                        "group": "model",
                        "vendor": "Anthropic",
                        "model": "claude-sonnet-4-6",
                        "detail": "1",
                    },
                )
                self.assertEqual(len(drill["records"]), 2)
                self.assertAlmostEqual(drill["totals"]["value"], 2.04, places=5)
                self.assertEqual(len(drill["entry"]), 1)
                self.assertAlmostEqual(drill["entry"][0]["value"], 2.04, places=5)
                self.assertEqual(len(drill["project"]), 1)
                self.assertAlmostEqual(drill["project"][0]["value"], 2.04, places=5)
                leaked = (*query.COST_PARTS, "p_cr_unk")
                for bag in (
                    drill["totals"],
                    *drill["trend"],
                    *drill["rank"],
                    *drill["rank"][0]["children"],
                    *drill["entry"],
                    *drill["project"],
                ):
                    for key in leaked:
                        self.assertNotIn(key, bag)
