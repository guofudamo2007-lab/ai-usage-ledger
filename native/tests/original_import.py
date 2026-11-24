"""Original-ledger migration checked with real Python storage and fictional data."""
import hashlib
import json
import shutil
import time
from contextlib import closing
from urllib.error import HTTPError
from ledger import core, imports, manual, pricing

def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mtime_ns

def rejected(action):
    try:
        action()
    except HTTPError as e:
        assert e.code == 400 and json.load(e)["error"]
    else:
        raise AssertionError("invalid or live database accepted")

def check(req, sync, path, rows, model, base, ui):
    note=dict(kind="subscription",entry="copilot",day=rows[0]["timestamp"][:10],
              project="验收",title="虚构手工订阅",amount="12.3400",currency="JPY",account="账户甲")
    rate=dict(model=model,input=3,output=10,cache_read=0,cache_write=3)
    with closing(core.connect(path)) as c:
        uid=manual.save(c,note)["id"]
        for item in (dict(note,kind="api_fee",entry="openai-api",currency="USD",amount="0"),
                     dict(note,kind="quota",amount="25",unit="次"),
                     dict(note,kind="media",entry="codex",media="已验收代码任务",produced=2,usable=1,retries=0,minutes=None)):
            manual.save(c,item)
        pricing.write_override(c,rate)
        # Original first saw the local AGY step; Rust first saw the stable request.
        anchor,request,old=c.execute("select anchor,request,uid from agy_keys limit 1").fetchone()
        assert old==request and anchor!=request
        c.execute("update events set uid=? where uid=?",(anchor,old))
        c.execute("update agy_keys set uid=? where uid=?",(anchor,old))
        c.execute("insert into annotations(uid,task) values(?,?)",(anchor,"接力验收"))
        c.commit()
    source_before=fingerprint(path)
    sidecars=lambda: sorted(f.name for f in path.parent.glob(path.name+"-*"))
    before_sidecars=sidecars()
    migrate=lambda p=path:req("/api/original/import",dict(path=str(p)))
    first=migrate()
    assert first["usage"]==dict(added=2,enriched=0,existing=2,skipped=0),first
    assert first["notes"]["added"]==4 and first["prices"]["added"]==1
    totals=lambda:req("/api/view?period=all")["totals"]
    assert (totals()["n"],totals()["total"])==(5,423)
    def saved_note():
        return next(n for n in req("/api/notes") if n["id"]==uid)
    assert saved_note()["amount"]=="12.3400" and saved_note()["account"]=="账户甲"
    manual_rows={n["kind"]:n for n in req("/api/notes") if n.get("account")=="账户甲"}
    assert manual_rows["api_fee"]["amount"]=="0" and manual_rows["quota"]["unit"]=="次"
    assert manual_rows["media"]["retries"]==0 and manual_rows["media"].get("minutes") is None
    prices=lambda:req("/api/pricing")["overrides"]
    assert prices()[model]["cache_read"]==0
    exports=req("/api/export?period=all&format=json")["records"]
    assert any(e["uid"]==request and e["task"]=="接力验收" for e in exports)
    assert any(e["total"]==50 and e["input"] is None for e in exports)
    assert any(e["total"]==0 and e["cache_read"]==0 for e in exports)
    revision=req("/api/status")["revision"]
    again=migrate()
    assert again["usage"]["existing"]==4 and again["notes"]["existing"]==4 and again["prices"]["existing"]==1
    assert all(again[key]["added"]==0 for key in ("usage","notes","prices"))
    assert req("/api/status")["revision"]==revision
    assert source_before==fingerprint(path) and before_sidecars==sidecars()
    # Duplicate API evidence and its undo must leave the imported baseline in place.
    batch=req("/api/import?format=json",rows)
    req("/api/undo",dict(batch=batch["batch"]))
    sync()
    assert (totals()["n"],totals()["total"])==(5,423)
    req("/api/notes",dict(note,id=uid,title="Rust 版更正"))
    req("/api/pricing",dict(rate,output=12))
    with closing(core.connect(path)) as c:
        imports.import_file(c,json.dumps([dict(rows[0],request_id="next",total=7)]).encode(),"json")
        rejected(migrate)
    copy=path.with_name("复制后改名的账本.sqlite3")
    shutil.copyfile(path,copy)
    src_hash=fingerprint(copy)
    delta=migrate(copy)
    assert delta["usage"]["added"]==1 and delta["notes"]["skipped"]==delta["prices"]["skipped"]==1,delta
    assert saved_note()["title"]=="Rust 版更正" and prices()[model]["output"]==12
    assert (totals()["n"],totals()["total"])==(6,430)
    if ui:
        from playwright.sync_api import sync_playwright,expect
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel="chrome",args=["--disable-background-networking"])
            page=browser.new_page()
            page.goto(base)
            page.locator('[data-nav="sources"]').click()
            page.get_by_role("button",name="导入原版账本").click()
            page.get_by_role("button",name="选择文件…").click()
            import win32gui,win32con
            deadline=time.monotonic()+8
            picker=0
            while not picker and time.monotonic()<deadline:
                picker=win32gui.FindWindow("#32770","选择原版账本或完整备份")
                time.sleep(.05)
            assert picker, "Windows file chooser did not open"
            try:
                from comtypes.client import CreateObject
                from comtypes.gen import UIAutomationClient as UI
                auto=CreateObject(UI.CUIAutomation,interface=UI.IUIAutomation)
                window=auto.ElementFromHandle(picker)
                while time.monotonic()<deadline:
                    namebox=window.FindFirst(4,auto.CreatePropertyCondition(30011,"1148"))
                    if namebox:break
                    time.sleep(.1)
                assert namebox, "Windows file name control missing"
                edit=namebox.FindFirst(4,auto.CreatePropertyCondition(30003,50004)) or namebox
                edit.GetCurrentPattern(UI.UIA_ValuePatternId).QueryInterface(UI.IUIAutomationValuePattern).SetValue(str(copy))
                assert edit.GetCurrentPattern(UI.UIA_ValuePatternId).QueryInterface(UI.IUIAutomationValuePattern).CurrentValue==str(copy)
                button=win32gui.GetDlgItem(picker,1)
                win32gui.PostMessage(button,win32con.BM_CLICK,0,0)
                expect(page.locator('#modal input[name="path"]')).to_have_value(str(copy))
            finally:
                if win32gui.IsWindow(picker):
                    win32gui.PostMessage(picker,win32con.WM_CLOSE,0,0)
            page.get_by_role("button",name="导入到 Rust 版").click()
            expect(page.locator("#modal [data-result]")).to_contain_text("导入完成")
            expect(page.locator("#modal [data-result]")).to_contain_text("新增 0")
            page.locator("#modal summary").click()
            expect(page.locator("#modal [data-result]")).to_contain_text("已有不同单价")
            browser.close()
    assert src_hash==fingerprint(copy)
    # A broken source discovered after the usage phase rolls back all new rows.
    with closing(core.connect(copy)) as c:
        imports.import_file(c,json.dumps([dict(rows[0],request_id="rollback",total=444)]).encode(),"json")
        c.execute("update notes set data='broken'")
        c.commit()
    damaged=fingerprint(copy)
    rejected(lambda:migrate(copy))
    assert (totals()["n"],totals()["total"])==(6,430) and fingerprint(copy)==damaged
    print("PASS original migration: read-only, overlapping AGY identities, manual notes, prices, repeat, incremental, conflicts, undo, WAL refusal, rollback"+(", browser import" if ui else ""),flush=True)
