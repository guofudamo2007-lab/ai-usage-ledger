import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ledger import pricing, query
from ledger.core import connect, counters, event, ingest, initialize, stamp


class PricingTests(unittest.TestCase):
    def test_builtin_matcher_and_unknown_guard(self):
        cases = [
            # Anthropic
            ("claude-fable-5-2", 8.0, 40.0),
            ("claude-fable-5-1", 10.0, 50.0),
            ("claude-opus-5-5", 4.0, 20.0),
            ("claude-opus-5", 5.0, 25.0),
            ("claude-opus-4-7", 5.0, 25.0),
            ("claude-opus-4-8", 5.0, 25.0),
            ("claude-opus-4-6", 5.0, 25.0),
            ("claude-4.6-opus", 5.0, 25.0),
            ("claude-opus-4-20250514", 15.0, 75.0),
            ("claude-3-opus-20240229", 15.0, 75.0),
            ("claude-sonnet-5-5", 2.0, 10.0),
            ("claude-5.5-sonnet", 2.0, 10.0),
            ("claude-sonnet-4-5-20250929", 3.0, 15.0),
            ("claude-haiku-5-5", 0.10, 0.50),
            ("claude-haiku-4-5", 1.0, 5.0),
            ("claude-3-haiku-20240307", 0.25, 1.25),
            # OpenAI
            ("gpt-6.2-astra", 8.0, 40.0),
            ("Astra 6.2", 8.0, 40.0),
            ("astra-6.2", 8.0, 40.0),
            ("gpt-astra-6.2", 8.0, 40.0),
            ("gpt-6.2-sol", 2.0, 10.0),
            ("Sol 6.2", 2.0, 10.0),
            ("gpt-6.1-terra", 1.0, 5.0),
            ("gpt-6-luna", 0.10, 0.50),
            ("Luna 6", 0.10, 0.50),
            ("gpt-5.6-sol", 4.0, 20.0),
            ("gpt-5.6-luna", 0.20, 1.20),
            ("gpt-5.5", 5.0, 30.0),
            ("gpt-5.4-pro", 30.0, 180.0),
            ("gpt-5.4", 2.50, 15.0),
            ("gpt-5.3-codex", 1.75, 14.0),
            ("gpt-5.3-codex-spark", 1.75, 14.0),
            ("gpt-5.3-codex-mini", 0.25, 2.0),
            ("gpt-5-mini", 0.25, 2.0),
            ("codex-mini-latest", 1.50, 6.0),
            ("gpt-oss-120b", 0.15, 0.60),
            ("chatgpt-4o-latest", 2.50, 10.0),
            ("gpt-4-turbo", 10.0, 30.0),
            ("o3-deep-research", 10.0, 40.0),
            ("o4-mini-deep-research", 2.0, 8.0),
            ("o4-mini", 1.10, 4.40),
            # Google Gemini
            ("gemini-4-carbon", 2.0, 10.0),
            ("gemini-4-argon", 2.0, 10.0),
            ("gemini-4-neon", 3.0, 15.0),
            ("gemini-4-hydrogen", 1.50, 7.50),
            ("gemini-3.8-flash", 0.75, 3.75),
            ("gemini-3.7-flash", 0.75, 3.75),
            ("gemini-3.6-flash", 0.75, 3.75),
            ("gemini-3.5-flash-lite", 0.30, 2.50),
            ("gemini-3.5-flash", 1.50, 9.0),
            ("gemini-3.1-flash-lite-preview", 0.25, 1.50),
            ("models/gemini-3.1-pro-preview", 2.0, 12.0),
            ("gemini-2.5-flash", 0.30, 2.50),
            ("gemini-2.0-flash-lite", 0.075, 0.30),
            ("gemini-1.5-pro", 1.25, 5.0),
            # xAI / Kimi / DeepSeek / MiniMax / GLM / Qwen
            ("grok-4.6", 2.0, 6.0),
            ("grok-4.20", 1.25, 2.50),
            ("grok-4.1-fast", 0.20, 0.50),
            ("grok-code-fast-1", 0.20, 1.50),
            ("grok-build-0.1", 1.0, 2.0),
            ("grok-2", 2.0, 10.0),
            ("kimi-k3.1-pro", 2.0, 10.0),
            ("kimi-k2.7-code", 0.95, 4.0),
            ("kimi-k2.5", 0.60, 2.50),
            ("kimi-for-coding", 0.60, 2.50),
            ("moonshot-v1-32k", 1.0, 3.0),
            ("deepseek-v4-pro", 0.66, 1.98),
            ("deepseek-flash", 0.15, 0.60),
            ("deepseek-r1", 0.55, 2.19),
            ("deepseek-chat", 0.28, 0.42),
            ("MiniMax-M3.1-Flash", 0.15, 0.60),
            ("MiniMax-M2.5", 0.30, 1.20),
            ("glm-5.3", 1.40, 4.40),
            ("glm-5", 1.0, 3.20),
            ("glm-4.7-flash", 0.0, 0.0),
            ("qwen4-max", 1.50, 4.50),
            ("qwen3.8-flash", 0.15, 0.47),
            ("qwen3-coder-plus", 1.0, 5.0),
            # Normalization formats (paths, Bedrock prefixes, enums, annotations)
            ("publishers/google/models/gemini-4-carbon", 2.0, 10.0),
            ("us.anthropic.claude-opus-4-7-v1:0", 5.0, 25.0),
            ("openrouter/openai/gpt-6.2-sol", 2.0, 10.0),
            ("MODEL_GEMINI_3_8_FLASH", 0.75, 3.75),
            ("Claude Opus 4.7 (Thinking)", 5.0, 25.0),
            ("claude-opus-4-6@20260205", 5.0, 25.0),
        ]
        for name, inp, out in cases:
            with self.subTest(model=name):
                rate = pricing.resolve(name)
                self.assertIsNotNone(rate)
                self.assertEqual(rate["input"], inp)
                self.assertEqual(rate["output"], out)

        # Unknown or placeholder model names must never receive fabricated prices.
        for unknown in (
            "gpt-fictional",
            "fictional-local-model",
            "cyber-muyu-pro-max",
            "AGY #42 · 型号未确认",
            "型号未记录",
            "",
            None,
        ):
            self.assertIsNone(pricing.resolve(unknown))

    def test_gpt6_standard_cache_write_and_model_generations(self):
        for model, expected in (
            ("gpt-6-astra", (10, 1, 12.5, 50)),
            ("gpt-6.2-astra", (8, .4, 10, 40)),
            ("gpt-6-sol", (2, .2, 2.5, 10)),
            ("gpt-6.1-sol", (2, .1, 2.5, 10)),
            ("gpt-6.1-terra", (1, .05, 1.25, 5)),
            ("gpt-6-luna", (.1, .01, .125, .5)),
        ):
            with self.subTest(model=model):
                rate = pricing.resolve(model)
                self.assertEqual(tuple(rate[k] for k in pricing.RATE_FIELDS), expected)
        self.assertIsNone(pricing.resolve("gpt-6.9-astra"))
        self.assertIsNone(pricing.resolve("Astra 6.9"))

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
