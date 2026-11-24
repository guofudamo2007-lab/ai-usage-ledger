"""Fictional Windows-compatible regression fixtures. No reads of configured client homes."""

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ledger import evidence, manual
from ledger.collect import sync
from ledger.core import connect, counters, initialize
from ledger.imports import import_file, undo
from ledger.query import records, sources, view
from test_ledger import proto, sample


class Reconciliation(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".qa"
        base.mkdir(exist_ok=True)
        tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.file = self.root / "ledger.sqlite3"
        initialize(self.file)
        self.db = connect(self.file)
        self.addCleanup(self.db.close)

    def put(self, rows, preview=False):
        return import_file(self.db, json.dumps(rows).encode(), "json", preview)

    def test_archived_inconsistent_counters_refresh_old_diagnostic(self):
        from compression import zstd

        logs = self.root / "fixture"
        logs.mkdir()

        def line(n, total):
            return (
                json.dumps(
                    {
                        "type": "event_msg",
                        "timestamp": f"2026-02-01T00:00:0{n}Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 0,
                                    "output_tokens": 0,
                                    "total_tokens": total,
                                }
                            },
                        },
                    }
                ).encode()
                + b"\n"
            )

        (logs / "rollout-fixture.jsonl.zst").write_bytes(zstd.compress(line(1, 0) + line(2, 19)))
        roots = {"codex": [logs]}
        first = sync(self.file, roots=roots)["codex"]
        self.assertEqual((first["added"], first["skipped"]), (1, 1))
        with self.db:
            self.db.execute("UPDATE files SET cursor=?", (json.dumps({"issues": ["ValueError"]}),))
        second = sync(self.file, roots=roots)["codex"]
        self.assertEqual((second["added"], second["skipped"]), (0, 1))
        details = self.db.execute("SELECT details FROM health WHERE entry='codex'").fetchone()[0]
        self.assertIn("输入与输出之和不等于总量", details)
        with patch("ledger.collect.read_codex", side_effect=AssertionError("unexpected replay")):
            self.assertEqual(sync(self.file, roots=roots)["codex"]["skipped"], 1)
        self.assertEqual(self.totals()["n"], 1)
        self.assertEqual(self.totals()["total"], 0)

    def totals(self, **args):
        return view(self.db, {"period": "all", **args})["totals"]

    def test_independent_coverage_and_reversible_evidence(self):
        base = [
            sample("a", model=None, project=None, input=100, output=20, cache_read=75, reasoning=5),
            sample("b", total=50),
            sample("c", input=0, output=0, cache_read=0),
        ]

        def verify():
            r = self.totals()
            for k, pair in dict(
                total=(170, 3),
                input=(100, 2),
                output=(20, 2),
                cache_read=(75, 2),
                reasoning=(5, 1),
                cache_write=(None, 0),
                net=(45, 2),
            ).items():
                self.assertEqual((r[k], r[k + "_n"]), pair)

        self.assertEqual(self.put(base, True)["added"], 3)
        self.assertEqual(self.totals()["n"], 0)
        first = self.put(base)["batch"]
        second = self.put(base)["batch"]
        enriched = self.put(sample("a", model="gpt-fictional", project="A", cache_write=0))
        self.assertEqual(enriched["enriched"], 1)
        self.assertEqual(self.totals()["cache_write_n"], 1)
        undo(self.db, first)
        undo(self.db, enriched["batch"])
        verify()
        self.assertEqual(self.totals(model="型号未记录", project="未归类")["n"], 1)
        undo(self.db, second)
        self.assertEqual(self.totals()["n"], 0)

    def test_limits_and_atomic_preview(self):
        for k in ("reasoning", "cache_write"):
            with self.assertRaises(ValueError):
                counters(total=10, **{k: 11})
            self.put(sample(k, total=10))
            rows = [sample("valid", total=4), sample(k, **{k: 11}), sample("", total=1)]
            p = self.put(rows, True)
            self.assertEqual((p["added"], p["conflict"], p["invalid"]), (1, 1, 1))
            with self.assertRaises(ValueError):
                self.put(rows)
            self.assertFalse(any(r["total"] == 4 for r in records(self.db, {"period": "all"})))
        # Individual cache subfields can overlap in the generic normalized contract.
        self.assertEqual(counters(input=10, cache_read=9, cache_write=8)["input"], 10)

    def test_source_health_and_codex_order_truncation(self):
        folder = self.root / "sessions"
        roots = {"codex": [folder]}

        def scan():
            sync(self.file, roots=roots)
            return next(r for r in sources(self.db) if r["key"] == "codex")

        self.assertEqual(scan()["health"], "尚未安装或配置")
        folder.mkdir()
        self.assertIn("暂无文件", scan()["health"])
        p = folder / "rollout-fictional.jsonl"

        def line(k, t, c):
            return json.dumps({"payload": c, "timestamp": t, "type": k}).encode() + b"\n"

        def usage(t, n):
            return line(
                "event_msg",
                t,
                {"info": {"last_token_usage": {"total_tokens": n}}, "type": "token_count"},
            )

        t = "2026-04-01T00:00:00Z"
        p.write_bytes(
            line("turn_context", t, {"model": "gpt-a", "turn_id": "turn-a"}) + usage(t, 8)
        )
        self.assertEqual(scan()["added"], 1)
        with p.open("ab") as f:
            f.write(line("event_msg", t, {"type": "token_count", "info": {"foo": True}}))
        self.assertEqual(scan()["skipped"], 1)
        p.write_bytes(usage("2026-04-02T00:00:00Z", 2))
        self.assertEqual(scan()["added"], 1)
        folder.rename(self.root / "archived")
        s = scan()
        self.assertEqual((s["health"], s["n"], s["readable"]), ("来源失联", 2, 0))
        self.assertIsNotNone(s["last_read"])
        (self.root / "archived").rename(folder)
        bad = folder / "rollout-bad.jsonl.zst"
        bad.write_bytes(b"fictional-invalid")
        self.assertEqual(scan()["health"], "文件访问失败")
        bad.unlink()
        self.assertEqual(scan()["health"], "可读取")

    def test_fork_only_then_parent_and_model_switch(self):
        folder = self.root / "sessions"
        folder.mkdir()

        def line(kind, when, payload):
            return (
                json.dumps(
                    dict(type=kind, timestamp=f"2026-02-01T00:00:{when:02}Z", payload=payload)
                )
                + "\n"
            )

        def tok(sec, n):
            return line(
                "event_msg",
                sec,
                {"type": "token_count", "info": {"last_token_usage": {"total_tokens": n}}},
            )

        prefix = line(
            "turn_context", 1, {"turn_id": "fictional-parent-turn", "model": "gpt-a"}
        ) + tok(2, 7)
        child = (
            line("session_meta", 4, {"id": "child"})
            + prefix
            + line("turn_context", 5, {"turn_id": "child-turn", "model": "gpt-b"})
            + tok(6, 11)
        )
        (folder / "rollout-child.jsonl").write_text(child, "utf-8")
        roots = {"codex": [folder]}
        self.assertEqual(sync(self.file, roots=roots)["codex"]["added"], 2)
        (folder / "rollout-parent.jsonl").write_text(
            line("session_meta", 0, {"id": "parent"}) + prefix, "utf-8"
        )
        self.assertEqual(sync(self.file, roots=roots)["codex"]["added"], 0)
        self.assertEqual(self.totals(model="gpt-a")["total"], 7)
        self.assertEqual(self.totals(model="gpt-b")["total"], 11)
        (folder / "rollout-unverified.jsonl").write_text(
            line("session_meta", 4, {"id": "other"}) + tok(2, 7), "utf-8"
        )
        self.assertEqual(sync(self.file, roots=roots)["codex"]["skipped"], 1)

    def test_agy_wal_observations_revisions_mirror_and_resolution(self):
        folder = self.root / "agy"
        folder.mkdir()
        p = folder / "fixture.db"
        src = sqlite3.connect(p)
        self.addCleanup(src.close)
        src.executescript("""PRAGMA journal_mode=WAL; CREATE TABLE trajectory_meta(trajectory_id);
            INSERT INTO trajectory_meta VALUES('test');CREATE TABLE steps(idx,metadata);CREATE TABLE gen_metadata(idx,data);""")

        def usage(n):
            return proto((2, n), (3, 2), (11, b"fixture-request"))

        def step(n):
            return proto((1, proto((1, 1700000000))), (9, usage(n)))

        src.execute("INSERT INTO steps VALUES(1,?)", (step(10),))
        src.commit()
        roots = {"agy": [folder]}
        self.assertEqual(sync(self.file, roots=roots)["agy"]["added"], 1)

        def gen(n):
            return proto((1, proto((4, usage(n)), (19, b"gemini-fixture"))), (2, 1))

        src.execute("INSERT INTO gen_metadata VALUES(1,?)", (gen(10),))
        src.commit()
        sync(self.file, roots=roots)
        self.assertEqual(self.totals(model="gemini-fixture")["total"], 12)
        src.execute("UPDATE gen_metadata SET data=?", (gen(15),))
        src.commit()
        self.assertEqual(sync(self.file, roots=roots)["agy"]["conflicts"], 1)
        src.close()
        other = self.root / "mirror"
        other.mkdir()
        (other / "mirror.db").write_bytes(p.read_bytes())
        sync(self.file, roots={"agy-cli": [other]})
        r = list(records(self.db, {"period": "all"}))
        self.assertEqual((len(r), r[0]["entry"], r[0]["total"]), (1, "agy-shared", 12))
        uid = r[0]["uid"]
        obs = evidence.review(self.db, uid)["observations"]
        pick = next(o for o in obs if o["record"]["total"] == 17)
        evidence.correct(
            self.db, dict(target=uid, action="resolve", value=pick["id"], reason="虚构验收终态")
        )
        self.assertEqual(self.totals()["total"], 17)
        self.assertEqual(evidence.alerts(self.db)["conflicts"], 0)
        self.assertEqual(sync(self.file, roots=roots)["agy"]["added"], 0)
        with closing(sqlite3.connect(p)) as revised:
            revised.execute("UPDATE gen_metadata SET data=?", (gen(10),))
            revised.commit()
        self.assertGreater(sync(self.file, roots=roots)["agy"]["conflicts"], 0)
        self.assertEqual(self.totals()["total"], 17)
        self.assertTrue(
            any(c["action"] == "观测修订" for c in evidence.review(self.db, uid)["changes"])
        )

    def test_accounts_projects_periods_and_filtered_pages(self):
        self.put(
            [
                sample(
                    str(i),
                    total=i,
                    project="copy" if i % 2 else "main",
                    timestamp="2026-03-31T15:30:00Z",
                )
                for i in range(65)
            ]
        )
        for raw in ("copy", "main"):
            evidence.correct(
                self.db, dict(action="alias", target=raw, value="Book", reason="虚构迁移")
            )
        uid = next(records(self.db, {"period": "all"}))["uid"]
        for action, value in (("account", "A"), ("task", "handoff")):
            evidence.correct(self.db, dict(action=action, value=value, target=uid, reason="核对"))
        self.assertEqual(self.totals(project="Book", task="handoff", account="A")["n"], 1)
        args = {"period": "all", "project": "Book", "detail": "1", "missing": "reasoning"}
        rows = list(records(self.db, args))
        v = view(self.db, args)
        self.assertEqual([*v["records"], *view(self.db, dict(args, page="1"))["records"]], rows)
        self.assertEqual(sum(r["total"] for r in rows), sum(r["total"] for r in v["rank"]))
        for zone, n in (("Asia/Tokyo", 65), ("Asia/Shanghai", 0), ("UTC", 0)):
            self.assertEqual(
                self.totals(period="custom", tz=zone, **{"from": "2026-04-01", "to": "2026-04-01"})[
                    "n"
                ],
                n,
            )
        for acc, kind in (("A", "subscription"), ("B", "api_fee"), ("A", "media")):
            manual.save(
                self.db,
                dict(
                    kind=kind,
                    entry="openai-api",
                    day="2026-04-01",
                    account=acc,
                    plan="Test",
                    cycle_start="2026-03-25",
                    cycle_end="2026-04-24",
                    media="已验收代码任务",
                    produced=2,
                    usable=1,
                    project="copy",
                    amount="2.5",
                    currency="USD" if acc == "A" else "JPY",
                ),
            )
        self.assertEqual(
            len(
                manual.list_notes(
                    self.db, dict(account="A", project="Book", cycle_start="2026-03-25")
                )
            ),
            2,
        )
        self.assertEqual(manual.list_notes(self.db, {"from": "2026-04-02"}), [])

    def test_legacy_snapshot_failure_rollback_and_undo(self):
        legacy = self.root / "old.sqlite3"
        with closing(sqlite3.connect(legacy)) as db:
            db.executescript("""CREATE TABLE events(uid TEXT PRIMARY KEY,ts INTEGER,entry TEXT,vendor TEXT,model TEXT,project TEXT,source TEXT,
                total INTEGER,input INTEGER,output INTEGER,cache_read INTEGER,cache_write INTEGER,reasoning INTEGER);
                CREATE TABLE files(id TEXT PRIMARY KEY,sig TEXT,skipped INTEGER,cursor TEXT);
                INSERT INTO files VALUES('test','[0,1]',2,'{}');PRAGMA user_version=3;""")
            from ledger.imports import convert

            db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                convert(sample("legacy", total=10)),
            )
            db.commit()
        with (
            patch.object(Path, "read_text", return_value="create table broken("),
            self.assertRaises(sqlite3.Error),
        ):
            initialize(legacy)
        with closing(connect(legacy)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertIsNone(
                db.execute("SELECT name FROM sqlite_master WHERE name='batches'").fetchone()
            )
        initialize(legacy)
        with closing(connect(legacy)) as db:
            raw = json.dumps(sample("legacy", input=7)).encode()
            batch = import_file(db, raw, "json")["batch"]
            undo(db, batch)
            r = next(records(db, {"period": "all"}))
            self.assertEqual((r["total"], r["input"]), (10, None))
            self.assertTrue(evidence.review(db, r["uid"])["legacy"])
            self.assertEqual(db.execute("SELECT cursor FROM files").fetchone()[0], "{}")
        for backup in (self.root / "backups").glob("*.sqlite3"):
            with closing(connect(backup)) as db:
                self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(db.execute("SELECT total FROM events").fetchone()[0], 10)

    def test_agy_late_request_id_keeps_local_identity(self):
        from ledger.evidence import identify_agy
        from ledger.core import digest, event, ingest

        anchor = digest("agy-local", "fictional-t", "step", 1)
        request = digest("agy-request", "fictional-r")

        def write(key, source):
            row = event(
                key,
                123,
                source,
                "Google",
                "gemini-fictional",
                "未归类",
                "AGY · steps.metadata",
                counters(total=2),
            )
            with self.db:
                rows, missed = identify_agy(
                    self.db, [row], [(anchor, None if key == anchor else key)]
                )
                self.assertEqual(missed, 0)
                return ingest(self.db, rows, source)

        self.assertEqual(write(anchor, "agy"), 1)
        self.assertEqual(write(request, "agy"), 0)
        self.assertEqual(write(request, "agy-cli"), 0)
        self.assertEqual(write(anchor, "agy"), 0)
        rec = next(records(self.db, {"period": "all"}))
        self.assertEqual(
            (rec["uid"], rec["entry"], self.totals()["total"]), (anchor, "agy-shared", 2)
        )

    def test_denied_directory_and_normal_messages(self):
        folder = self.root / "logs"
        folder.mkdir()
        (folder / "rollout-fictional.jsonl").write_text(
            json.dumps(
                dict(type="event_msg", payload=dict(type="agent_message", message="fictional"))
            )
            + "\n",
            "utf-8",
        )
        roots = {"codex": [folder]}
        with patch("ledger.collect.os.scandir", side_effect=PermissionError("denied")):
            self.assertEqual(sync(self.file, roots=roots)["codex"]["failures"], 1)
        self.assertEqual(sync(self.file, roots=roots)["codex"]["skipped"], 0)
        self.assertEqual(self.totals()["n"], 0)

    def test_workspace_path_alias_before_first_handoff(self):
        from ledger.core import event, ingest, project_name

        for path in ("D:/fixture/main", "D:/fixture/worktrees/feature"):
            evidence.correct(
                self.db, dict(action="alias", path=path, value="虚构作品", reason="核对工作区")
            )
        for uid, path, entry in (
            ("first", "d:/fixture/MAIN", "codex"),
            ("second", "D:/fixture/worktrees/feature", "agy"),
            ("third", "D:/fixture/main", "codex"),
        ):
            with self.db:
                ingest(
                    self.db,
                    [
                        event(
                            uid,
                            100,
                            "codex" if entry == "codex" else "agy",
                            "Test",
                            "fake",
                            project_name(path),
                            "虚构验收",
                            counters(total=8),
                        )
                    ],
                )
            evidence.correct(
                self.db, dict(target=uid, action="task", value="同一需求", reason="接力测试")
            )
        args = {"period": "all", "project": "虚构作品", "task": "同一需求"}
        data = view(self.db, {**args, "group": "entry"})
        self.assertEqual(
            sorted((r["entry"], r["total"]) for r in data["rank"]), [("agy", 8), ("codex", 16)]
        )
        self.assertEqual(len({r["raw_project"] for r in records(self.db, args)}), 2)
