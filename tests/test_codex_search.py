"""Fictional current and former profiles; no personal data is accessed."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ledger import codex_search
from ledger.collect import sync
from ledger.core import connect, initialize


class CodexSearchTest(unittest.TestCase):
    def test_cli_opens_separate_codex_only_ledger(self):
        app = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=app/".qa") as tmp:
            base = Path(tmp)
            profile = base/"fictional profile"
            (profile/"sessions").mkdir(parents=True)
            output = base/"data"
            for _ in range(2):
                result = subprocess.run([sys.executable,"-B","-X","utf8",str(app/"ledger_app.py"),
                    "--codex-home",str(profile),"--data-dir",str(output),"--collect"],
                    check=True,capture_output=True)
                self.assertEqual(set(json.loads(result.stdout)),{"codex"})
            self.assertFalse((output/"ledger.sqlite3").exists())
            files = list(output.glob("codex-profiles/*/ledger.sqlite3"))
            self.assertEqual(len(files),1)
            db = connect(files[0])
            try:
                self.assertEqual(codex_search.current_root(db),profile)
            finally:
                db.close()

    def test_recover_current_indexed_files_without_importing_old_profile(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/".qa") as temp:
            base = Path(temp)
            home, old, backup = base/".codex", base/".codex-old", base/"backup"
            for folder in (home/"sessions", old/"sessions", backup):
                folder.mkdir(parents=True)
            ids = [f"aaaaaaaa-0000-0000-0000-{i:012d}" for i in range(4)]
            def write(folder, i):
                folder.mkdir(parents=True, exist_ok=True)
                path = folder/f"rollout-{ids[i]}.jsonl"
                when = "2026-01-01T00:00:00Z"
                usage = {"total_tokens":100+i, "input_tokens":90+i, "output_tokens":10}
                rows = [("session_meta", {"id":ids[i], "timestamp":when}),
                        ("event_msg", {"type":"token_count", "info":{"last_token_usage":usage, "total_token_usage":usage}})]
                path.write_text("".join(json.dumps({"timestamp":when,"type":t,"payload":p})+"\n" for t,p in rows), encoding="utf-8")
                return path
            session = write(home/"sessions", 0)
            moved = write(home/"relocated", 1)
            recovered = write(backup, 2)
            foreign = write(old/"sessions", 3)
            shutil.copy2(foreign, backup/foreign.name)
            shutil.copy2(session, backup/session.name)
            fake = write(backup/"wrong", 3)
            fake.rename(fake.with_name(recovered.name))
            index = home/"state_5.sqlite"
            db = sqlite3.connect(index)
            db.execute("create table threads(id text, rollout_path text, tokens_used integer)")
            db.executemany("insert into threads values(?,?,?)", [(ids[i],str(p),100+i) for i,p in enumerate((session,moved,home/recovered.name))])
            db.commit()
            db.close()
            file = base/"ledger.sqlite3"
            initialize(file)
            with patch("pathlib.Path.home", return_value=base), patch.dict(os.environ,{"CODEX_HOME":str(home)}):
                sync(file)
                ledger = connect(file)
                try:
                    def total():
                        return ledger.execute("select count(*), sum(total) from events where entry='codex'").fetchone()[:]
                    self.assertEqual(total(), (2,201))
                    before = codex_search.status(ledger)
                    self.assertEqual((before["missing"],before["total"]), (1,102))
                    self.assertIn(str(old),before["last"]["others"])
                    with self.assertRaises(ValueError):
                        codex_search.set_backup(ledger,str(old))
                    codex_search.set_backup(ledger,str(backup))
                    with patch.dict(os.environ,{"CODEX_HOME":str(old)}):
                        sync(file)
                        sync(file)
                        self.assertEqual(codex_search.current_root(ledger),home)
                        self.assertEqual(total(),(3,303))
                        self.assertEqual(codex_search.status(ledger)["missing"],0)
                        self.assertIn("部分补查文件无法核实会话身份",codex_search.status(ledger)["last"]["warnings"])
                        index.rename(home/"unavailable.sqlite")
                        sync(file)
                        self.assertEqual(total(),(3,303))
                        self.assertTrue(codex_search.status(ledger)["last"]["warnings"])
                finally:
                    ledger.close()
