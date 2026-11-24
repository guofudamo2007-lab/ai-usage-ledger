"""Windows parity check on two temporary ledgers and isolated fictional sources."""
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
from zoneinfo import reset_tzpath
if (ROOT / "runtime/tzdata/zoneinfo").is_dir():
    reset_tzpath([str(ROOT / "runtime/tzdata/zoneinfo")])
sys.path.insert(0, str(ROOT))
from ledger import collect, core, imports, query
from tests.test_ledger import proto

def wait(f, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = f()
            if value:
                return value
        except OSError:
            pass
        time.sleep(0.1)
    raise AssertionError(f)

def run(exe, root):
    app = root / "便携 Rust 版"
    app.mkdir()
    exe = Path(shutil.copy2(exe, app / "Kedu-Rust.exe"))
    home = root / "fixture-home"
    sessions = home / ".codex/sessions"
    agy = home / ".gemini/antigravity/conversations"
    mirror = home / ".gemini/antigravity-ide/conversations"
    for directory in (sessions, agy, mirror):
        directory.mkdir(parents=True)
    ts = datetime.now(timezone.utc).isoformat()
    model = "gpt-6.2-sol"
    u = dict(total_tokens=120,input_tokens=100,output_tokens=20,cached_input_tokens=75,reasoning_output_tokens=5)
    lines = [("session_meta",dict(id="session-fixture",model_provider="openai")),("turn_context",dict(turn_id="turn-fixture",model=model,cwd=str(root/"fictional"))),("event_msg",dict(type="token_count",info=dict(last_token_usage=u,total_token_usage=u)))]
    (sessions/"rollout-fixture.jsonl").write_text("".join(json.dumps(dict(timestamp=ts,type=k,payload=v))+"\n" for k,v in lines),"utf-8")
    path = agy/"fixture.db"
    with closing(sqlite3.connect(path)) as src:
        src.executescript("create table steps(idx,metadata);create table gen_metadata(idx,data);create table trajectory_meta(trajectory_id);insert into trajectory_meta values('fictional');")
        stats = proto((1,345),(2,20),(3,10),(4,3),(5,100),(11,b"fixture"))
        src.execute("insert into steps values(1,?)",(proto((1,proto((1,int(time.time())))),(9,stats)),))
        src.execute("insert into gen_metadata values(2,?)",(proto((1,proto((4,stats),(19,b"gemini-3.1-pro"))),(2,1)),))
        src.commit()
    shutil.copy2(path,mirror/path.name)
    env = dict(os.environ,USERPROFILE=str(home),HOME=str(home),CODEX_HOME=str(sessions.parent),LOCALAPPDATA=str(home),APPDATA=str(home))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    args=[str(exe),"--no-sync","--no-browser","--port",str(port)]
    base=f"http://127.0.0.1:{port}"
    def req(path,data=None):
        with urlopen(Request(base+path,data=json.dumps(data).encode() if data is not None else None,headers={"X-Ledger-Local":"1"}),timeout=10) as reply:
            return json.load(reply)
    def start():
        p=subprocess.Popen(args,env=env,cwd=root)
        try: wait(lambda:req("/api/instance")["app"]=="kedu-native")
        except BaseException:
            p.terminate()
            p.wait()
            raise
        return p
    def sync():
        req("/api/sync",{})
        wait(lambda:not req("/api/status")["running"])
    p=start()
    try:
        sync()
        pyfile=root/"comparison.sqlite3"
        core.initialize(pyfile)
        collect.sync(pyfile,roots={"codex":[sessions],"agy":[agy],"agy-ide":[mirror]})
        rows=[dict(schema="meter.usage.v1",entry="openai-api",model=model,timestamp=ts,request_id="total-only",total=50),dict(schema="meter.usage.v1",entry="openai-api",model=model,timestamp=ts,request_id="zero",input=0,output=0,cache_read=0)]
        preview=req("/api/import?format=json&preview=1",rows)
        assert preview["added"]==2,preview
        batch=req("/api/import?format=json",rows)
        duplicate=req("/api/import?format=json",rows)
        assert duplicate["duplicate"]==2
        with closing(core.connect(pyfile)) as c:
            imports.import_file(c,json.dumps(rows).encode(),"json")
            expected=query.view(c,{"period":"all","rankings":"1","tz":"Asia/Tokyo"})
            observed=req("/api/view?period=all&rankings=1&tz=Asia/Tokyo")
            for k,v in expected["totals"].items():
                got=observed["totals"][k]
                assert abs(got-v)<1e-7 if isinstance(v,float) else got==v,(k,got,v,req("/api/sources"))
            assert observed["totals"]["n"]==4,observed
            for dim in ("model","entry","vendor"):
                assert [(r[dim],r["n"],r["total"]) for r in observed["rankings"][dim]]==[(r[dim],r["n"],r["total"]) for r in expected["rankings"][dim]]
            exported=req("/api/export?period=all&format=json&tz=UTC")["records"]
            assert {r["uid"] for r in exported}=={r["uid"] for r in query.records(c,{"period":"all","tz":"UTC"})}
        req("/api/undo",{"batch":batch["batch"]})
        assert req("/api/view?period=all")["totals"]["n"]==4
        req("/api/undo",{"batch":duplicate["batch"]})
        sync()
        assert req("/api/view?period=all")["totals"]["n"]==2
        value=req("/api/widget?period=today&tz=UTC")
        assert value["totals"]["total"]==253
        req("/api/pricing",dict(model=model,input=10,cache_read=1,cache_write=10,output=20))
        adjusted=req("/api/view?period=all")
        assert abs(next(r["value"] for r in adjusted["pricing"]["models"] if r["model"]==model)-0.000725)<0.000001
        req("/api/pricing",dict(model=model,reset=True))
        quota=req("/api/subscription/claude-code?import=1",{"rate_limits":{"five_hour":{"used_percentage":25}}})
        assert quota["windows"][0]["remaining"]==75
        assert req("/api/widget?period=quota")["quotas"][0]["windows"][0]["remaining"]==75
        req("/api/notes",dict(kind="subscription",entry="copilot",title="虚构验收",project="演示",day=ts[:10],account="虚构甲",currency="JPY",amount="200"))
        assert len(req("/api/notes?account="+__import__("urllib.parse").parse.quote("虚构甲")))==1
        assert not req("/api/notes?account=other")
        if "--ui" in sys.argv:
            from playwright.sync_api import sync_playwright,expect
            import win32gui,win32con
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel="chrome",args=["--disable-background-networking"])
                page=browser.new_page()
                errors=[]
                page.on("pageerror",lambda err:errors.append(str(err)))
                page.goto(base+"/?period=all")
                expect(page.locator("#rank")).to_contain_text(model)
                page.locator(".name-btn").first.click()
                expect(page.locator("#back")).to_be_visible()
                page.locator("#back").click()
                page.locator("#open-widget").click()
                hwnd=wait(lambda:win32gui.FindWindow(None,"刻度 · 桌面小组件 "+str(port)))
                from comtypes.client import CreateObject,GetModule
                GetModule("UIAutomationCore.dll")
                from comtypes.gen import UIAutomationClient as UI
                auto=CreateObject(UI.CUIAutomation,interface=UI.IUIAutomation)
                widget=auto.ElementFromHandle(hwnd)
                try:
                    browser.close()
                    wait(lambda:widget.FindFirst(4,auto.CreatePropertyCondition(30005,"253")))
                    assert req("/api/widget?period=week&tz=UTC")["totals"]["total"]==253
                finally:
                    win32gui.PostMessage(hwnd,win32con.WM_CLOSE,0,0)
                assert not errors,errors
        req("/api/stop",{})
        p.wait(8)
        p=start()
        assert req("/api/view?period=all")["totals"]["total"]==253
        # Append half a real usage envelope, resume and restart without a duplicate.
        stamp2=datetime.now(timezone.utc).isoformat()
        event=dict(timestamp=stamp2,type="event_msg",payload=dict(type="token_count",info=dict(last_token_usage=u,total_token_usage={"total_tokens":240})))
        encoded=(json.dumps(event)+"\n").encode()
        logfile=sessions/"rollout-fixture.jsonl"
        with logfile.open("ab") as out:out.write(encoded[:42])
        sync()
        assert req("/api/view?period=all")["totals"]["total"]==253
        with logfile.open("ab") as out:out.write(encoded[42:])
        sync()
        assert req("/api/view?period=all")["totals"]["total"]==373
        subprocess.run(args,env=env,cwd=root,timeout=10,check=True)
        sync()
        assert req("/api/view?period=all")["totals"]["total"]==373
        with socket.socket() as sock:
            sock.bind(("127.0.0.1",0))
            old_base=f"http://127.0.0.1:{sock.getsockname()[1]}"
        old_args=[sys.executable, "-B", str(ROOT/"ledger_app.py"), "--no-browser", "--no-sync",
                  "--data-dir", str(root/"原版独立空账本"), "--port", old_base.rsplit(":",1)[1]]
        def old_read(path):
            with urlopen(old_base+path,timeout=3) as reply:
                return json.load(reply)
        old=subprocess.Popen(old_args,env=dict(env,PYTHONTZPATH=str(ROOT/"runtime/tzdata/zoneinfo")),cwd=ROOT)
        try:
            wait(lambda:old_read("/api/instance")["app"]=="meter")
            assert old_read("/api/view?period=all")["totals"]["n"]==0
            assert req("/api/view?period=all")["totals"]["total"]==373
            req("/api/stop",{})
            p.wait(8)
            assert old_read("/api/instance")["app"]=="meter"
        finally:
            if old.poll() is None:
                try:
                    with urlopen(Request(old_base+"/api/stop",data=b"{}",headers={"X-Ledger-Local":"1"}),timeout=3):
                        pass
                    old.wait(8)
                finally:
                    if old.poll() is None:
                        old.terminate()
                        old.wait()
        print("PASS simultaneous Python and Rust services, separate databases and independent shutdown",flush=True)
        print("PASS native executable, Codex and mirrored AGY, API import/undo, UID, totals, coverage, ranking, prices, export, restart"+(", Chromium + WPF" if "--ui" in sys.argv else ""),flush=True)
    finally:
        if p.poll() is None:
            try: req("/api/stop",{})
            finally:
                try:p.wait(8)
                except subprocess.TimeoutExpired:p.kill();p.wait()

if __name__=="__main__":
    qa=ROOT/".qa/rust-native"
    qa.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=qa) as temp:
        run(Path(sys.argv[1]).resolve(),Path(temp))

