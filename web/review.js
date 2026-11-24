import { $, $$, api, post, esc, full, toast, option, metNames } from "./ui.js";

const fields = ["total","input","output","cache_read","cache_write","reasoning"];
const counts = r => fields.map(k => metNames[k] + " " + full(r[k])).join(" · ");
const modal = () => $("#modal");
function sheet(title, content) {
  const box = $("#modal-content");
  box.innerHTML = `<div class="dialog-head"><h2>${esc(title)}</h2><button type="button" aria-label="关闭">×</button></div><div class="review-body">${content}</div>`;
  $("button",box).onclick = () => modal().close();
  if (!modal().open) modal().showModal();
  return box;
}

export function openOriginalImport(refresh) {
  const box = sheet("从原版导入账本", `
    <p>先在原版「数据接入」停止本地服务，再选择 <code>private/ledger.sqlite3</code>，也可以使用已关闭的完整备份。</p>
    <form><label>原版账本文件<input name="path" required autocomplete="off" placeholder="选择文件或粘贴完整路径"></label>
    <button class="outline" type="button" data-pick>选择文件…</button>
    <button class="primary" type="submit">导入到 Rust 版</button></form>
    <p class="muted">只读原文件。相同记录去重，已知值冲突时保留 Rust 版。</p>
    <div role="status" data-result></div>`);
  const form = $("form", box), pick = $("[data-pick]", form), result = $("[data-result]", box);
  pick.onclick = async () => {
    pick.disabled = true;
    try {
      const path = await post("/api/original/pick", {});
      if (path) form.elements.path.value = path;
    } catch (err) { result.textContent = err.message; }
    finally { pick.disabled = false; }
  };
  form.onsubmit = async e => {
    e.preventDefault();
    const submit = e.submitter || $("button[type=submit]", form);
    submit.disabled = pick.disabled = true;
    result.textContent = "正在核对并导入…";
    try {
      const report = await post("/api/original/import", {path: form.elements.path.value});
      result.innerHTML = "<h3>导入完成</h3>" + [["usage","历史用量"],["notes","手工账目"],["prices","自定义单价"]].map(([key,label]) => {
        const row = report[key];
        return `<p><b>${label}</b>　新增 ${full(row.added)} · 已有 ${full(row.existing)}${row.enriched ? " · 补全 "+full(row.enriched) : ""} · 跳过 ${full(row.skipped)}</p>`;
      }).join("") + (report.details.length ? `<details><summary>查看跳过原因（最多 30 条）</summary>${report.details.map(d=>`<p>${esc(d)}</p>`).join("")}</details>` : "");
      await refresh().catch(err=>toast(err.message));
    } catch (err) { result.textContent = "未写入本次导入。"+err.message; }
    finally { submit.disabled = pick.disabled = false; }
  };
}

export async function inspect(uid, refresh) {
  try {
    const d = await api("/api/evidence?uid=" + encodeURIComponent(uid));
    const r = d.current;
    const box = sheet("记录与依据",`
      ${r ? `<p>${esc(r.model)} · ${esc(r.project)} · ${esc(r.account)} · ${esc(r.task)}</p><p>${counts(r)}</p>
      <small>原始项目 · ${esc(r.raw_project)}　来源 · ${esc(r.source)}</small>` : "<p>当前未入账</p>"}
      ${d.legacy ? "<p>旧版来源未记录</p>" : ""}
      ${d.observations.map(o => `<p><b>#${o.id} · ${esc(o.entry)} · ${esc(o.source)}${o.latest?" · 本来源最近观测":""}</b><br>${counts(o.record)}<br>${esc(o.record.model)} · ${esc(o.record.project)}<br><small>${esc(o.seen)} · 来源标识 ${esc(o.origin.slice(0,12))}${o.origin.includes(":")?" · 行 "+esc(o.origin.split(":")[1]):""}</small></p>`).join("")}
      ${d.batches.map(b => `<p>批次 #${b.id} · 第 ${b.line} 行 · ${b.undone ? "已撤销" : "有效"}<br>${counts(b.record)}</p>`).join("")}
      ${r ? `<form class="review-form"><label>更正项目<select name="action">${[["project","项目"],["account","账号"],["task","接力任务"],...(d.disputed && d.observations.length?[["resolve","采用指定观测编号"]]:[])].map(([v,l])=>option(v,l)).join("")}</select></label>
      <label>新值<input name="value" maxlength="160" placeholder="留空恢复来源归属"></label><label hidden data-pick>采用观测<select name="observation">${d.observations.map(o=>option(o.id,`#${o.id} · ${o.source} · ${full(o.record.total)} Token`)).join("")}</select></label><label>修改依据<input name="reason" maxlength="300" required></label><button class="outline">保存更正</button></form>` : ""}
      ${d.changes.map(c=>`<p>${esc(c.at)} · ${esc(c.action)} · ${esc(c.previous)} → ${esc(c.value)}<br>${esc(c.reason)}</p>`).join("")}`);
    const form = $("form",box);
    if (form) form.elements.action.onchange=()=>{const resolving=form.elements.action.value==="resolve";form.elements.value.parentElement.hidden=resolving;form.querySelector("[data-pick]").hidden=!resolving;};
    if (form) form.onsubmit = async e => {
      e.preventDefault();
      try {
        const data=Object.fromEntries(new FormData(form));
        if(data.action==="resolve") data.value=data.observation;
        await post("/api/correct",{...data,target:uid});
        await refresh(); await inspect(uid,refresh);
      } catch(err) {toast(err.message);}
    };
  } catch(err) {toast(err.message);}
}

async function batchSheet(batch, page, refresh) {
  try {
    const d=await api(`/api/batch?id=${batch}&page=${page}`);
    const labels={added:"新增",enriched:"补全",duplicate:"重复"};
    const box=sheet("导入批次 #"+batch,`<p>${d.batch.undone?"已撤销":"有效"} · ${esc(d.batch.created)}</p><small>文件 SHA-256 · ${esc(d.batch.fingerprint)}</small>
      ${d.rows.map(r=>`<p>第 ${r.line} 行 · ${labels[r.outcome]} · ${esc(r.record.model)} · 导入总量 ${full(r.record.total)}<br>
      <button class="text-btn" data-uid="${esc(r.uid)}">${r.present?"当前总量 "+full(r.effective_total):"已从账本移除"} · 查看依据</button></p>`).join("")}
      <button class="small-btn" id="prev-line" ${page?"":"disabled"}>上一页</button> <button class="small-btn" id="next-line" ${d.more?"":"disabled"}>下一页</button>`);
    $("#prev-line",box).onclick=()=>batchSheet(batch,page-1,refresh);
    $("#next-line",box).onclick=()=>batchSheet(batch,page+1,refresh);
    for (const el of $$("[data-uid]",box)) el.onclick=()=>inspect(el.dataset.uid,refresh);
  } catch(err){toast(err.message);}
}

export function manage(root, refresh) {
  const panel = $(".management",root);
  let page = 0;
  async function load() {
    try {
      const d = await api("/api/manage?page="+page);
      const area = $(".review-body",panel);
      area.innerHTML = `<h3>导入批次</h3>${d.batches.map(b=>{
        const r=JSON.parse(b.result);
        return `<p>#${b.id} · ${esc(b.created)} · 新增 ${r.added} / 重复 ${r.duplicate} / 补全 ${r.enriched}
          <button class="text-btn" data-batch="${b.id}">核对逐行结果</button> ${b.undone?"已撤销":`<button class="small-btn" data-undo="${b.id}">撤销批次</button>`}</p>`;
      }).join("") || "<p>尚无可撤销批次。旧版来源未记录。</p>"}
      ${d.batch_count>d.limit?`<button class="small-btn" id="batch-prev" ${page===0?"disabled":""}>上一页</button> <button class="small-btn" id="batch-next" ${(page+1)*d.limit>=d.batch_count?"disabled":""}>下一页</button>`:""}
      <h3>观测冲突</h3>${d.disputes.map(s=>`<p><button class="text-btn" data-uid="${esc(s.uid)}">${esc(s.model)} · ${esc(s.reason)} · ${s.resolved?"已核对":"待核对"}</button></p>`).join("") || "<p>没有待核对的观测</p>"}
      <h3>项目归并</h3><form class="review-form"><label>原始项目<select name="target">${d.projects.map(p=>option(p,p+(d.aliases[p]?" → "+d.aliases[p]:""))).join("")}</select></label><label>或指定工作区路径<input name="path" placeholder="完整路径（选填）" maxlength="4096"></label><label>逻辑项目<input name="value" placeholder="留空取消归并" maxlength="160"></label><label>依据<input name="reason" required maxlength="300"></label><button class="outline">保存归并</button></form>
      ${d.changes.filter(c=>c.action==="alias").map(c=>`<p>${esc(c.target)} → ${esc(c.value)} · ${esc(c.reason)}</p>`).join("")}`;
      for (const el of $$("[data-batch]",area)) el.onclick=()=>batchSheet(el.dataset.batch,0,refresh);
      for (const el of $$("[data-uid]",area)) el.onclick=()=>inspect(el.dataset.uid,refresh);
      for (const el of $$("[data-undo]",area)) el.onclick=async()=>{
        if (!confirm("撤销这个批次，并按剩余证据重算相关记录？")) return;
        try { const r=await post("/api/undo",{batch:Number(el.dataset.undo)}); await refresh();
          toast(`已撤销：移除 ${r.removed} 条，保留 ${r.retained} 条，其中 ${r.recomputed} 条已重算`);
        } catch(err){toast(err.message);}
      };
      $("form",area).onsubmit=async e=>{
        e.preventDefault();
        try { await post("/api/correct",{action:"alias",...Object.fromEntries(new FormData(e.target))}); await refresh(); toast("已保存项目归并"); }
        catch(err){toast(err.message);}
      };
      if ($("#batch-prev",area)) {
        $("#batch-prev",area).onclick=()=>{page--; load();}; $("#batch-next",area).onclick=()=>{page++; load();};
      }
    } catch(err){toast(err.message);}
  }
  panel.ontoggle=()=>{if(panel.open) load();};
}

export async function previewFile(file,format,refresh) {
  const send = preview => api("/api/import?format="+format+(preview?"&preview=1":""),{method:"POST",headers:{"X-Ledger-Local":"1"},body:file});
  const r = await send(true);
  const box=sheet("导入预检",`<p>${esc(file.name)}</p><p>新增 ${r.added} · 重复 ${r.duplicate} · 可补全 ${r.enriched} · 冲突 ${r.conflict} · 无效 ${r.invalid}</p>
    <p>API 按厂商与原始请求 ID 去重。若文件也包含 Codex 或 AGY 已采集的调用，请先从文件排除这些行。</p>
    ${r.problems.map(t=>`<p>${esc(t)}</p>`).join("")}
    <button class="primary" id="apply-import" ${r.conflict || r.invalid?"disabled":""}>确认整批导入</button>`);
  $("#apply-import",box).onclick=async e=>{
    e.target.disabled=true;
    try { const result=await send(false); modal().close(); await refresh(); toast("已入账 · 批次 #"+result.batch); }
    catch(err){toast(err.message); $("#apply-import",box).disabled=false;}
  };
}

