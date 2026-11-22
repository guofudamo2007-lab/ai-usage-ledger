"""All fixtures in this file are fictional. Test outputs live on the project drive."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from compression import zstd

from ledger import manual
from ledger.collect import sync
from ledger.core import connect, counters, event, ingest, initialize, project_name
from ledger.imports import import_file
from ledger.query import records, revision, sources, view


def vi(n):
    out = bytearray()
    while n > 127:
        out.append((n & 127) | 128)
        n >>= 7
    out.append(n)
    return bytes(out)


def proto(*pairs):
    return b"".join(
        vi(k * 8) + vi(v) if isinstance(v, int) else vi(k * 8 + 2) + vi(len(v)) + v
        for k, v in pairs
    )


def sample(rid, **kwargs):
    return dict(
        {
            "schema": "meter.usage.v1",
            "entry": "openai-api",
            "request_id": rid,
            "model": "fictional-v1",
            "timestamp": "2026-03-08T08:00:00Z",
            "project": "虚构项目",
        },
        **kwargs,
    )


class LedgerTest(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".qa"
        base.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.root = Path(self.tmp.name)
        self.file = self.root / "ledger.sqlite3"
        initialize(self.file)
        self.db = connect(self.file)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def put(self, rows):
        return import_file(self.db, json.dumps(rows).encode(), "json")

    def test_native_openai_and_same_id_from_csv(self):
        payload = {
            "schema": "meter.response.v1",
            "entry": "openai-api",
            "timestamp": "2026-03-08T08:00:00Z",
            "response": {
                "id": "fictional-a",
                "model": "fictional-v1",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 70},
                    "output_tokens_details": {"reasoning_tokens": 6},
                    "total_tokens": 120,
                },
            },
        }
        self.assertEqual(self.put(payload)["added"], 1)
        csv = b"schema,entry,request_id,model,timestamp,total,input,output,cache_read\\nmeter.usage.v1,openai-api,fictional-a,fictional-v1,2026-03-08T08:00:00Z,120,100,20,70\\n".replace(
            b"\\n", b"\n"
        )
        self.assertEqual(import_file(self.db, csv, "csv")["duplicate"], 1)
        t = view(self.db, {"period": "all"})["totals"]
        self.assertEqual((t["n"], t["total"], t["net"], t["reasoning"]), (1, 120, 50, 6))

    def test_missing_partial_refresh_and_atomic_conflict(self):
        self.put(sample("fictional-a", total=120))
        a = view(self.db, {"period": "all"})["totals"]
        self.assertEqual((a["input"], a["net"], a["net_n"]), (None, None, 0))
        self.put(sample("fictional-a", cache_read=20))
        with self.assertRaises(ValueError):
            self.put([sample("fictional-b", output=30), sample("fictional-a", input=10)])
        a = view(self.db, {"period": "all"})["totals"]
        self.assertEqual((a["n"], a["input"], a["net"]), (1, None, 100))
        self.assertIsNone(view(self.db, {"period": "all", "entry": "kimi-api"})["totals"]["total"])

    def test_concurrent_import(self):
        raw = json.dumps(sample("fictional-race", total=20)).encode()

        def run(_):
            d = connect(self.file)
            try:
                return import_file(d, raw, "json")
            finally:
                d.close()

        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(run, range(2)))
        self.assertEqual(sum(r["added"] for r in results), 1)
        self.assertEqual(sum(r["duplicate"] for r in results), 1)

    def test_revision_tracks_new_and_enriched_usage_only(self):
        original = sample("fictional-snapshot", total=100)
        self.assertEqual(revision(self.db), 0)
        self.put(original)
        self.assertEqual(revision(self.db), 1)
        self.put(original)
        self.assertEqual(revision(self.db), 1)
        self.put({**original, "cache_read": 35})
        new = view(self.db, {"period": "all", "rankings": "1"})
        self.assertEqual((new["revision"], new["totals"]["net"]), (2, 65))
        with self.assertRaises(ValueError):
            self.put([sample("fictional-rollback", total=1), {**original, "total": 90}])
        self.assertEqual(revision(self.db), 2)
        for rows in new["rankings"].values():
            self.assertEqual(sum(r["net"] for r in rows), 65)
            self.assertTrue(all(r["output"] is None and r["output_n"] == 0 for r in rows))

    def test_distinct_workspaces_and_corrupt_archive(self):
        refs = [
            project_name(p)
            for p in ("D:/fictional-A/app", "/D:/fictional-B/app", "d:\\fictional-a\\APP")
        ]
        with self.db:
            ingest(
                self.db,
                [
                    event(
                        str(i),
                        i + 1,
                        "codex",
                        "OpenAI",
                        "fictional",
                        ref,
                        "测试",
                        counters(total=1),
                    )
                    for i, ref in enumerate(refs)
                ],
            )
        labels = [r[0] for r in self.db.execute("SELECT project FROM events ORDER BY uid")]
        self.assertEqual(labels[0], labels[2])
        self.assertNotEqual(labels[0], labels[1])
        self.assertNotIn("fictional-A", str(labels))
        bad = self.root / "rollout-fictional.jsonl.zst"
        bad.write_bytes(b"fictional invalid archive")
        self.assertEqual(sync(self.file, roots={"codex": [self.root]})["codex"]["failures"], 1)

    def test_gemini_and_claude(self):
        g = {
            "schema": "meter.response.v1",
            "entry": "ai-studio",
            "timestamp": "2026-03-08T08:00:00Z",
            "response": {
                "responseId": "fictional-g",
                "modelVersion": "models/fictional-g",
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 10,
                    "cachedContentTokenCount": 75,
                    "thoughtsTokenCount": 5,
                    "toolUsePromptTokenCount": 3,
                    "totalTokenCount": 118,
                },
            },
        }
        c = {
            "schema": "meter.response.v1",
            "entry": "claude-api",
            "timestamp": "2026-03-08T08:00:00Z",
            "response": {
                "id": "fictional-c",
                "model": "fictional-c",
                "usage": {
                    "input_tokens": 10,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 20,
                    "output_tokens": 7,
                },
            },
        }
        self.put([g, c])
        r = {x["entry"]: x for x in records(self.db, {"period": "all"})}
        self.assertEqual(
            (r["ai-studio"]["total"], r["ai-studio"]["output"], r["ai-studio"]["model"]),
            (118, 15, "fictional-g"),
        )
        self.assertEqual((r["claude-api"]["total"], r["claude-api"]["net"]), (87, 37))
        del c["response"]["usage"]["cache_read_input_tokens"]
        c["response"]["id"] = "fictional-c2"
        self.put(c)
        self.assertEqual(
            view(self.db, {"period": "all", "entry": "claude-api"})["totals"]["total_n"], 1
        )

    def test_provenance_and_validation(self):
        for invalid in [
            sample("r", entry="prism", total=1),
            sample("r", entry="codex", total=1),
            sample("r", entry="chatgpt-web", total=1),
            sample("r", estimated=True, total=1),
            sample("", total=2),
            sample("r", timestamp="2026-01-01", total=1),
            sample("r", input=12, output=2, total=16),
            sample("r", total=-1),
            sample("r", total=2, cache_read=3),
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.put(invalid)

    def test_dst_boundaries_coverage_and_reconciliation(self):
        for i, t in enumerate(
            [
                "2026-03-08T04:59:59Z",
                "2026-03-08T05:00:00Z",
                "2026-03-09T03:59:59.999999Z",
                "2026-03-09T04:00:00Z",
            ]
        ):
            self.put(
                sample(
                    "fictional-" + str(i),
                    timestamp=t,
                    total=100,
                    cache_read=75 if i != 2 else None,
                    model="fictional-v1" if i != 2 else "fictional-v2",
                )
            )
        args = {
            "period": "custom",
            "from": "2026-03-08",
            "to": "2026-03-08",
            "tz": "America/New_York",
        }
        for g in ("model", "vendor", "entry"):
            data = view(self.db, {**args, "sort": "net", "group": g})
            self.assertEqual(data["totals"]["total"], 200)
            self.assertEqual(data["totals"]["net_n"], 1)
            self.assertEqual(data["totals"]["net"], 25)
            self.assertEqual(sum(r["total"] for r in data["rank"]), 200)
            self.assertEqual(sum(r["total"] for r in data["trend"]), 200)
            self.assertEqual(sum(r["total"] for r in records(self.db, args)), 200)
        self.assertEqual(view(self.db, {**args, "tz": "UTC"})["totals"]["n"], 2)

    def test_agy_protobuf_step_generation_and_mirror(self):
        p = self.root / "fictional.db"
        src = sqlite3.connect(p)
        src.executescript(
            "create table steps(idx,metadata);create table gen_metadata(idx,data);create table trajectory_meta(trajectory_id);insert into trajectory_meta values ('fictional-t');"
        )
        u = proto(
            (1, 345),
            (2, 20),
            (3, 10),
            (4, 3),
            (5, 100),
            (9, 4),
            (10, 6),
            (11, b"fictional-request"),
        )
        src.execute("insert into steps values(1,?)", (proto((1, proto((1, 1700000000))), (9, u)),))
        src.execute("insert into steps values(2,?)", (proto((1, proto((1, 1700000001)))),))
        src.execute(
            "insert into gen_metadata values(1,?)",
            (proto((1, proto((4, u), (19, b"gemini-fictional"))), (2, vi(1))),),
        )
        src.commit()
        src.close()
        dest = self.root / "mirror"
        dest.mkdir()
        (dest / "another.db").write_bytes(p.read_bytes())
        r = sync(self.file, roots={"agy": [self.root], "agy-cli": [dest]})
        self.assertEqual(r["agy"]["added"], 1)
        self.assertEqual(r["agy-cli"]["added"], 0)
        t = view(self.db, {"period": "all"})["totals"]
        self.assertEqual(
            (t["total"], t["input"], t["output"], t["cache_read"], t["net"]),
            (133, 123, 10, 100, 33),
        )
        self.assertEqual(sync(self.file, roots={"agy": [self.root]})["agy"]["added"], 0)

    def test_codex_incremental_cumulative_compression_and_fork(self):
        folder = self.root / "sessions"
        folder.mkdir()
        p = folder / "rollout-fictional.jsonl"

        def line(tag, payload, second):
            return (
                json.dumps(
                    {
                        "timestamp": f"2026-02-01T00:00:{second:02d}Z",
                        "ordinal": second,
                        "type": tag,
                        "payload": payload,
                    }
                )
                + "\n"
            ).encode()

        part = {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "cached_input_tokens": 80,
            "reasoning_output_tokens": 8,
        }

        def tok(sec, multiple):
            return line(
                "event_msg",
                {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": part,
                        "total_token_usage": {"total_tokens": 120 * multiple},
                    },
                },
                sec,
            )

        prefix = line("session_meta", {"id": "fictional-s", "model_provider": "openai"}, 0) + line(
            "turn_context",
            {"turn_id": "fictional-turn", "model": "gpt-fictional", "cwd": "/test/fictional"},
            1,
        )
        p.write_bytes(prefix + tok(2, 1) + tok(3, 1))
        roots = {"codex": [folder]}
        self.assertEqual(sync(self.file, roots=roots)["codex"]["added"], 1)
        with p.open("ab") as f:
            f.write(tok(4, 2)[:-2])
        self.assertEqual(sync(self.file, roots=roots)["codex"]["added"], 0)
        with p.open("ab") as f:
            f.write(tok(4, 2)[-2:])
        self.assertEqual(sync(self.file, roots=roots)["codex"]["added"], 1)
        (folder / "rollout-copy.jsonl.zst").write_bytes(zstd.compress(p.read_bytes()))
        self.assertEqual(sync(self.file, roots=roots)["codex"]["added"], 0)
        fork = line(
            "session_meta", {"id": "fictional-child", "timestamp": "2026-02-01T00:00:10Z"}, 10
        )
        (folder / "rollout-fork.jsonl").write_bytes(fork + tok(2, 1) + tok(3, 1))
        self.assertEqual(sync(self.file, roots=roots)["codex"]["added"], 0)
        t = view(self.db, {"period": "all"})["totals"]
        self.assertEqual(
            (t["n"], t["total"], t["input"], t["output"], t["reasoning"], t["net"]),
            (2, 240, 200, 40, 16, 80),
        )

    def test_manual_separate_from_tokens_and_reopen(self):
        note = {
            "day": "2026-01-01",
            "entry": "gemini-web",
            "kind": "media",
            "media": "视频",
            "produced": 5,
            "usable": 3,
            "amount": "2.90",
            "currency": "USD",
            "title": "虚构测试",
        }
        uid = manual.save(self.db, note)["id"]
        manual.save(self.db, {**note, "id": uid, "usable": 4})
        for kind in ("subscription", "api_fee", "quota"):
            manual.save(self.db, {**note, "kind": kind, "unit": "点"})
        self.assertEqual(len(manual.list_notes(self.db)), 4)
        self.assertEqual(view(self.db, {"period": "all"})["totals"]["n"], 0)
        self.assertEqual(
            next(s for s in sources(self.db) if s["key"] == "gemini-web")["manual_n"], 4
        )
        with self.assertRaises(ValueError):
            manual.save(self.db, {**note, "usable": 6})
        initialize(self.file)
        self.assertEqual(len(manual.list_notes(self.db)), 4)


if __name__ == "__main__":
    unittest.main()
