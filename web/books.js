import {manage,previewFile} from "./review.js";
import {
  $,
  $$,
  api,
  post,
  esc,
  icon,
  option,
  full,
  timeAt,
  empty,
  toast,
} from "./ui.js";

const modes = {
  auto: "自动采集",
  import: "可导入",
  manual: "仅手填",
  none: "暂不支持",
  derived:"待确认入口",
};
export function renderSources(
  root,
  rows,
  { tz, pageHead, syncButton, doSync, refresh, diagnosis },
) {
  function item(r) {
    let status;
    if (r.health) status = r.health;
    else if (r.n) status = r.failures || r.skipped || r.missing_logs ? "已接入 · 部分覆盖" : "已接入";
    else if (r.mode === "auto")
      status = r.failures
        ? "未接入 · 只读访问失败"
        : r.present
          ? "已检查 · 无实测记录"
          : "未接入";
    else
      status =
        r.mode === "import"
          ? "等待导入"
          : r.mode === "manual"
            ? "仅可手工记账"
            : "不计入 Token";
    const coverage = r.historical || r.n
      ? `${timeAt(r.first, tz, true)} → ${timeAt(r.last, tz, true)}`
      : "尚无 Token 覆盖";
    return `<article data-source="${esc(r.key)}"><div><h3>${esc(r.name)}</h3><span class="source-state ${r.failures || r.skipped || r.health==="来源失联" || r.conflicts ? "warn" : !r.n ? "off" : ""}">${esc(status)}</span></div><div><b>${esc(r.method)}</b><small>${r.files != null ? `发现 ${r.files} 份 · ${r.failures || 0} 处读取失败 · ${r.skipped || 0} 条未纳入` : esc(r.platform)}</small></div>
    <div><span class="time-text">${coverage}</span><small>${r.n ? full(r.n) + " 条归属此入口" : "历史覆盖时段"}${r.observed ? " · 本来源观测到 " + full(r.observed) + " 条" : ""}</small>${r.manual_n ? `<small>手填 ${r.manual_first} → ${r.manual_last} · ${r.manual_n} 条</small>` : ""}</div>
    <div><span class="time-text">${timeAt(r.checked || r.manual_updated, tz)}</span><small>${r.checked ? "最近检查 / 导入" : r.manual_updated ? "最近手工更新" : "最近更新"}</small>${r.mode==="auto" ? `<small>最近成功读取 · ${r.last_read?timeAt(r.last_read,tz):"尚未记录"}</small><small>最新事件 · ${r.last?timeAt(r.last,tz):"尚未入账"}</small>
    <small>本次可读 ${full(r.readable)} · 新增 ${full(r.added)} · 补全 ${full(r.enriched)} · 冲突 ${full(r.conflicts)}</small>
    ${r.details?.length?`<details><summary>未纳入或读取失败</summary>${r.details.map(([f,m])=>`<small>${esc(f)} · ${esc(m)}</small>`).join("")}</details>`:""}`:""}</div></article>`;
  }
  const searchPanel = '<details class="guide codex-search"><summary>Codex 搜寻范围与缺失历史</summary><div class="search-body"></div></details>';
  root.innerHTML =
    pageHead(
      "数据接入",
      `<span id="sync-status">读取本机来源</span>`,
      syncButton(),
    ) +
    `<div class="import-panel"><div><h3>导入逐请求用量</h3><p>选择本地 JSON 或 CSV。账本按原始请求标识去重。</p></div><button id="pick" class="outline">${icon("upload")}选择文件</button><input type="file" id="import-file" accept=".csv,.json,application/json,text/csv" hidden></div>
    <details class="guide management"><summary>核对导入批次、观测冲突与项目归并</summary><div class="review-body"></div></details>
    <details class="guide"><summary>查看文件格式与计数口径</summary><p>标准明细每行一个请求，必填原始请求标识、入口和带时区的发生时间。模型缺失将单独列出。输入包含缓存，输出包含推理。无记录的字段留空或填 null。</p>
    <pre>schema,entry,request_id,timestamp,model,project,total,input,output,cache_read,cache_write,reasoning</pre>
    <p>可用入口 <code>openai-api / gemini-api / ai-studio / claude-api / grok-api / kimi-api</code>。标准记录版本为 <code>meter.usage.v1</code>。厂商响应可包装为 <code>meter.response.v1</code>，完整示例和对应字段见随附《使用说明》。</p>
    <p>AGY 原始输入分列新输入、缓存读写。Codex 的输入已含读取缓存。只有总量与缓存读取均已知的记录参与剔除缓存后的排行。Prism 汇总保留作对账参考。</p></details>
    ${Object.entries(modes)
      .map(
        ([mode, text]) =>
          `<section class="source-group"><div class="section-head"><div class="title-line"><h2>${text}</h2><span class="section-count">${rows.filter((x) => x.mode === mode).length} 个入口</span></div><small class="muted">${esc(tz)}</small></div><div class="source-list">${rows
            .filter((x) => x.mode === mode)
            .map(item)
            .join("")}</div>${mode === "auto" ? searchPanel : ""}</section>`,
      )
      .join("")}<button class="outline" id="stop-server">停止本地服务</button>`;
  manage(root,refresh);
  const search = $(".codex-search", root), body = $(".search-body", search);
  search.ontoggle = async () => {
    if (!search.open) return;
    try {
      const s = await api("/api/codex-search");
      const last = s.last;
      body.innerHTML = `<p>当前账本的 Codex 目录 <code>${esc(s.home)}</code></p>
        <p>搜索会话、归档、索引指向的本目录日志，以及本目录的备份和迁移记录。</p>
        ${last?.others?.length ? `<p>其他配置目录 · 当前账本已排除。可在程序目录运行以下命令查看独立账本。</p>${last.others.map(p=>`<p><code>.\\启动账本.cmd --codex-home '${esc(p.replaceAll("'","''"))}'</code></p>`).join("")}` : ""}
        ${last?.warnings?.length ? `<p>${esc(last.warnings.join("；"))}</p>` : ""}
        ${s.threads ? `<p>线程索引累计 <b>${s.index_total == null ? "未知" : full(s.index_total) + " Token"}</b> · ${full(s.counted)} / ${full(s.threads)} 个会话有累计值</p>` : ""}
        ${last && (s.missing || !last.warnings.length) ? `<p>${s.missing ? `缺少日志 · ${full(s.missing)} 个索引会话；其中 ${full(s.known)} 个有累计值，共 ${s.total == null ? "未知" : full(s.total) + " Token"}。仅供对账。` : "索引会话均已找到日志。"}</p>` : ""}
        <form class="backup-search"><label>当前账号的额外备份目录<input name="backup" autocomplete="off" value="${esc(s.backup)}" placeholder="本地备份文件夹的完整路径"></label>
        <button class="outline">保存目录</button></form><p>保存后点「更新用量」。备份只匹配当前索引或账本曾保存的会话 ID。</p>`;
      $("form",body).onsubmit=async e=>{
        e.preventDefault();
        try { await post("/api/codex-search",{backup:e.currentTarget.elements.backup.value}); toast("目录已保存"); }
        catch(err){ toast(err.message); }
      };
    } catch(err) { body.textContent=err.message; }
  };
  if (diagnosis) {
    const target=diagnosis==="conflicts" ? $(".management",root) : $$("[data-source]",root).find(el=>el.dataset.source===diagnosis);
    if (target) {
      if (target.tagName==="DETAILS") target.open=true;
      const details=$("details",target);
      if(details) details.open=true;
      if(diagnosis==="codex" && rows.find(r=>r.key==="codex")?.missing_logs) search.open=true;
      target.scrollIntoView({block:"center"});
    }
  }
  $("#stop-server").onclick = async () => {
    if (confirm("停止本地账本服务？可双击启动账本再次打开。")) {
      try {
        await post("/api/stop", {});
        root.innerHTML = empty(
          "账本已关闭",
          "双击“启动账本.cmd”即可再次打开。",
        );
      } catch (err) {
        toast(err.message);
      }
    }
  };
  $("#sync").onclick = doSync;
  $("#pick").onclick = () => $("#import-file").click();
  $("#import-file").onchange = async (e) => {
    const f = e.target.files[0];
    if (!f) return;
    if (f.size > 32 * 1024 * 1024) {
      toast("单个文件上限 32 MiB，请按请求拆分");
      return;
    }
    $("#pick").disabled = true;
    try {
      const fmt = f.name.toLowerCase().endsWith(".csv") ? "csv" : "json";
      await previewFile(f,fmt,refresh);
      $("#pick").disabled=false;
      e.target.value="";
    } catch (err) {
      toast(err.message);
      $("#pick").disabled = false;
      e.target.value = "";
    }
  };
}

const kinds = {
  media: "产出批次",
  subscription: "订阅费",
  api_fee: "API 费用",
  quota: "剩余额度",
};
const tabs = Object.entries(kinds);

function quotaPeriod({ minutes, slot }) {
  if (!minutes) return slot === "primary" ? "主要窗口" : "次要窗口";
  if (minutes % 1440 === 0) return `${minutes / 1440} 天额度`;
  if (minutes % 60 === 0) return `${minutes / 60} 小时额度`;
  return `${minutes} 分钟额度`;
}

function quotaPanel(provider, title) {
  return `<section class="book-panel quota-panel" data-quota="${provider}"><header class="section-head"><div><h2>${title}</h2><small class="quota-stamp muted"></small></div><button class="outline">查询额度</button></header><div class="quota-windows"></div><p role="status"></p></section>`;
}

function agyPeriod(group, label) {
  const period = label.startsWith(group + " ") ? label.slice(group.length + 1) : label;
  return ({ "Weekly Limit Remaining": "每周额度", "Five Hour Limit Remaining": "5 小时额度",
    "Weekly Limit": "每周额度", "Five Hour Limit": "5 小时额度" })[period] || label;
}

function subscriptionDirectory(services) {
  return `<section class="book-panel subscriptions"><header class="section-head"><h2>订阅入口</h2></header>${["对话", "编程", "创作"].map(category => {
    const rows = Object.entries(services).filter(([,s]) => s.category === category);
    return `<details><summary>${category}<small>${rows.length} 个入口</small></summary><div class="sub-directory">${rows.map(([id,s]) => `<div class="sub-item"><div><b>${esc(s.name)}</b><small>${s.automatic ? "自动读取" : s.importable ? "可导入快照" : "手工记录"} · ${esc(s.hint)}</small></div><div class="sub-actions"><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">官方入口 ↗</a>${s.importable ? `<button data-quota-file="${id}">导入</button>` : ""}<button data-quota-note="${id}">记录</button></div></div>`).join("")}</div></details>`;
  }).join("")}<input type="file" class="quota-file" accept=".json,application/json" hidden></section>`;
}

async function loadQuota(panel, tz, services, codexOnly) {
  const provider = panel.dataset.quota, service = services[provider];
  const auto = provider === "agy" || (provider === "codex" && !codexOnly) || service?.automatic;
  const endpoint = service ? `/api/subscription/${provider}` : `/api/${provider}-quota`;
  const button = $("button", panel), body = $(".quota-windows", panel);
  const stamp = $(".quota-stamp", panel), status = $("[role=status]", panel);
  function showSnapshot(snapshot) {
    if (!snapshot) {
      status.textContent = auto ? "正在读取额度…" : service ? "尚无额度快照。" : "查询当前 Codex 配置的订阅额度。";
      return;
    }
    const stampLabel = ({"client-log":"客户端记录", import:"导入时间", file:"文件更新时间"})[snapshot.source] || "上次查询";
    stamp.textContent = `${stampLabel} ${timeAt(snapshot.at, tz)} · ${tz}`;
    status.textContent = "";
    function windowHtml(w, title) {
      const expired = w.reset != null && w.reset * 1000 <= Date.now();
      const reset = w.reset ? timeAt(w.reset * 1000, tz) : null;
      const known = Number.isFinite(w.remaining);
      const state = expired ? "周期已结束" : w.state || (!known ? "未提供" : "");
      return `<article><span>${esc(title)}</span>
        ${state ? `<strong class="quota-ended">${esc(state)}</strong>` : `<strong>${w.remaining}<small>% 剩余</small></strong><div class="quota-track" aria-hidden="true"><i style="width:${Math.min(100,Math.max(0,w.remaining))}%"></i></div>`}
        <small>${reset ? `${expired ? "重置于" : "重置时间"} ${reset}` : "重置时间未提供"}</small></article>`;
    }
    if (provider === "agy") {
      const groups = new Map();
      for (const w of snapshot.windows) {
        if (!groups.has(w.group)) groups.set(w.group, []);
        groups.get(w.group).push(w);
      }
      body.innerHTML = [...groups].map(([group, rows]) => `<div class="quota-group"><h3>${esc(group)}</h3>
        <div>${rows.map(w => windowHtml(w, agyPeriod(group, w.label))).join("")}</div></div>`).join("");
    } else {
      body.innerHTML = snapshot.windows.map(w => windowHtml(w, w.label || quotaPeriod(w))).join("");
    }
  }
  async function query(live, force = false) {
    button.disabled = true;
    button.textContent = live ? "查询中…" : "查询额度";
    try {
      const queryString = force ? "?force=1" : provider === "codex" ? "?auto=1" : "";
      showSnapshot(await (live ? post(endpoint + queryString, {}) : api(endpoint)));
    } catch (error) {
      status.textContent = error.message;
    } finally {
      button.disabled = false;
      button.textContent = "查询额度";
    }
  }
  button.onclick = () => query(true, true);
  if (service && !service.automatic) button.hidden = true;
  await query(false);
  if (auto && panel.isConnected) {
    async function update() {
      if (!panel.isConnected) return;
      if (!document.hidden && !button.disabled) await query(true);
      if (panel.isConnected) setTimeout(update, 300000);
    }
    await update();
  }
}
const show = (v) => (v == null ? "—" : esc(v));
const money = (n) =>
  n.amount == null
    ? "—"
    : esc(n.amount) + " " + esc(n.kind === "quota" ? n.unit : n.currency);
function summary(rows, tab) {
  if (!rows.length) return "";
  const totals = [];
  if (tab === "media") {
    for (const medium of new Set(rows.map(r=>r.media))) {
      const items = rows.filter((n) => n.media === medium);
      if (items.length)
        totals.push([
          medium + " · 可用 / 总产出",
          items.reduce((s, n) => s + n.usable, 0) +
            " / " +
            items.reduce((s, n) => s + n.produced, 0),
        ]);
    }
  } else if (tab === "subscription" || tab === "api_fee") {
    const sums = new Map();
    for (const row of rows) {
      const key = kinds[row.kind] + " · " + row.currency;
      const [whole, fraction = ""] = row.amount.split(".");
      const minor =
        BigInt(whole) * 100000000n + BigInt(fraction.padEnd(8, "0"));
      sums.set(key, (sums.get(key) || 0n) + minor);
    }
    for (const [key, v] of sums) {
      const digits = v.toString().padStart(9, "0"),
        fractional = digits.slice(-8).replace(/0+$/, "");
      totals.push([
        key,
        digits.slice(0, -8) + (fractional ? "." + fractional : ""),
      ]);
    }
  }
  return totals.length
    ? `<div class="total-chips">${totals.map(([name, n]) => `<div class="total-chip"><span>${esc(name)}</span><b>${esc(n)}</b></div>`).join("")}</div>`
    : "";
}
export function renderBook(
  root,
  all,
  { registry, pageHead, tz, booktab, navigate, refresh },
) {
  const tab = tabs.some(([v]) => v === booktab) ? booktab : "media";
  const isCost = tab === "subscription" || tab === "api_fee";
  const search = new URLSearchParams(location.search);
  const bookFilters = [["from","起始日期","date"],["to","截止日期","date"],["project","项目","text"],["account","账号","text"],["plan","套餐","text"],["cycle_start","周期起始","date"],["cycle_end","周期截止","date"]];
  const selected = bookFilters.some(([key])=>search.get("book_"+key));
  const rows = all.filter((n) => n.kind === tab);
  const services = registry.subscriptions || {};
  const title = tabs.find((v) => v[0] === tab)[1];
  const add = tab === "media" ? "记一批产出" : "记录" + title;
  const cols =
    tab === "media"
      ? [
          "日期 / 入口",
          "批次 / 项目",
          "类型",
          "可用 / 总产出",
          "重试",
          "修整时长",
          "实际花费",
        ]
      : isCost
        ? ["日期", "入口", "账目 / 项目", "实付金额"]
        : ["记录日期", "入口", "额度名称 / 项目", "当时剩余"];
  function row(n) {
    const entry = esc(services[n.entry]?.name || registry.entries[n.entry] || n.entry),
      tag = `${esc(n.title || kinds[n.kind])}<small>${esc(n.logical_project || n.project || "未归类")}</small><small>${esc(n.account || "账号未确认")} · ${esc(n.plan || "套餐未记录")}${n.cycle_start ? " · "+esc(n.cycle_start)+" → "+esc(n.cycle_end) : ""}</small>`;
    const cells =
      tab === "media"
        ? [
            esc(n.day) + "<small>" + entry + "</small>",
            tag,
            esc(n.media),
            `${n.usable} / ${n.produced}`,
            show(n.retries),
            n.minutes == null ? "—" : n.minutes + " 分钟",
            money(n),
          ]
        : [esc(n.day), entry, tag, money(n)];
    return `<tr>${cells.map((v) => `<td>${v}</td>`).join("")}<td><div class="book-tools"><button data-edit="${esc(n.id)}">编辑</button><button data-remove="${esc(n.id)}">删除</button></div></td></tr>`;
  }
  const previousTabs = $(".pills", root);
  root.innerHTML =
    pageHead(
      "产出账本",
      "按批次记录可用成果、返工与实际花费。",
      `<button class="primary" id="add-note">${icon("plus")}${add}</button>`,
    ) +
    `<details class="guide" ${selected?"open":""}><summary>${selected?"当前筛选范围":"全部历史"} · 费用与产出筛选</summary><form id="book-filter" class="review-form">${bookFilters.map(([k,l,t])=>`<label>${l}<input name="book_${k}" type="${t}" value="${esc(search.get("book_"+k)||"")}"></label>`).join("")}<button class="outline">应用</button><button type="button" class="text-btn" id="clear-book">全部历史</button></form></details><div class="switch pills" role="group" aria-label="账目类型" style="--count:4;--index:${tabs.findIndex(([k])=>k===tab)}">${tabs.map(([v, t]) => `<button data-book="${v}" aria-pressed="${v===tab}" class="${v === tab ? "selected" : ""}">${t}</button>`).join("")}</div>${summary(rows, tab)}
    ${tab === "quota" ? quotaPanel("codex", "Codex 订阅额度") + (registry.codex_profile ? "" : quotaPanel("agy", "Antigravity 订阅额度") + Object.entries(services).filter(([,s]) => s.automatic || s.saved).map(([id,s]) => quotaPanel(id, esc(s.name))).join("") + subscriptionDirectory(services)) : ""}
    <section class="book-panel"><div class="section-head"><div class="title-line"><h2>${tab === "quota" ? "手工额度记录" : title}</h2><span class="section-count">${rows.length} 条</span></div></div>
    ${rows.length ? `<div class="scroll-table"><table><thead><tr>${[...cols, "操作"].map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${rows.map(row).join("")}</tbody></table></div>` : tab === "quota" ? '<p class="quota-manual">可在右上角记录其他订阅的剩余额度。</p>' : empty("尚无" + title, tab === "media" ? "第一批图、视频或音频完成后，记下总数、可用数和返工时间。" : "按账单上的实付金额与币种登记。", `<button id="empty-add" class="outline">${icon("plus")}${add}</button>`)}</section>
    ${tab === "quota" ? "" : `<p class="book-note">${isCost ? "按币种分别汇总。产出批次的花费只随批次展示。" : "批次花费可以留空。订阅账单与 API 费用在各自页签登记。"}</p>`}`;
  $("#book-filter").onsubmit=e=>{e.preventDefault();navigate(Object.fromEntries(new FormData(e.target)));};
  $("#clear-book").onclick=()=>navigate(Object.fromEntries(bookFilters.map(([k])=>["book_"+k,null])));
  for (const panel of $$(".quota-panel", root)) loadQuota(panel, tz, services, registry.codex_profile);
  for (const button of $$("[data-quota-note]", root))
    button.onclick = () => noteForm(null, tab, registry, tz, refresh, button.dataset.quotaNote);
  const filePicker = $(".quota-file", root);
  for (const button of $$("[data-quota-file]", root))
    button.onclick = () => { filePicker.dataset.provider = button.dataset.quotaFile; filePicker.click(); };
  if (filePicker) filePicker.onchange = async () => {
    const file = filePicker.files[0], key = filePicker.dataset.provider;
    if (!file) return;
    try {
      if (file.size > 1024 * 1024) throw new Error("额度 JSON 上限 1 MiB");
      await api(`/api/subscription/${key}?import=1`, {method:"POST", headers:{"X-Ledger-Local":"1","Content-Type":"application/json"}, body:file});
      services[key].saved = true;
      await refresh();
      toast("额度快照已导入");
    } catch (err) { toast(err.message); }
    filePicker.value = "";
  };
  if (previousTabs) {
    $(".pills",root).replaceWith(previousTabs);
    previousTabs.style.setProperty("--index", tabs.findIndex(([id]) => id === tab));
    for (const el of $$("[data-book]",previousTabs)) {
      const selected = el.dataset.book === tab;
      el.setAttribute("aria-pressed",String(selected));
      el.classList.toggle("selected",selected);
    }
  }
  for (const el of $$("[data-book]"))
    el.onclick = () => navigate({ booktab: el.dataset.book });
  const open = (n) => noteForm(n, tab, registry, tz, refresh);
  $("#add-note").onclick = () => open(null);
  if ($("#empty-add")) $("#empty-add").onclick = () => open(null);
  for (const el of $$("[data-edit]"))
    el.onclick = () => open(all.find((r) => r.id === el.dataset.edit));
  for (const el of $$("[data-remove]"))
    el.onclick = async () => {
      if (confirm("删除这条手工记录？")) {
        try {
          await post("/api/notes/remove", { id: el.dataset.remove });
          await refresh();
        } catch (err) {
          toast(err.message);
        }
      }
    };
}
function noteForm(previous, tab, registry, tz, refresh, selectedEntry) {
  const modal = $("#modal"),
    idx = previous || {},
    kind = idx.kind || tab;
  const uid = idx.id || crypto.randomUUID();
  const input = (
    name,
    label,
    type = "text",
    required = false,
    value = idx[name] ?? "",
    attrs = "",
  ) =>
    `<label><span>${label}</span><input name="${name}" type="${type}" value="${esc(value)}" ${required ? "required" : ""} ${attrs}></label>`;
  const select = (name, label, list, selected) =>
    `<label><span>${label}</span><select name="${name}">${list.map(([v, l]) => option(v, l, selected)).join("")}</select></label>`;
  const names = {...registry.entries};
  if (kind === "quota" || kind === "subscription")
    for (const [key,s] of Object.entries(registry.subscriptions || {})) names[key] = s.name;
  const entries = Object.entries(names).filter(
    ([k]) => k !== "prism",
  );
  $("#modal-content").innerHTML =
    `<form id="note-form"><div class="dialog-head"><h2>${previous ? "编辑记录" : "记录" + kinds[kind]}</h2><button type="button" id="close-note" aria-label="关闭">${icon("x")}</button></div>
  <div class="form-grid">${input("day", "记录日期", "date", true, idx.day || timeAt(new Date(), tz, true))}${select("entry", "使用入口", entries, idx.entry || selectedEntry)}
  ${input("title", tab === "media" ? "批次名称（选填）" : "账目名称（选填）", "text", false, idx.title || "", 'maxlength="160"')}${input("project", "项目（选填）", "text", false, idx.project || "", 'maxlength="160"')}
  ${
    tab === "media"
      ? select(
          "media",
          "产出类型",
          ["图像","视频","音频","完成章节","可用稿件","设计方案","资料整理","已验收代码任务"].map((x) => [x, x]),
          idx.media,
        ) +
        input(
          "produced",
          "总产出数量",
          "number",
          true,
          idx.produced ?? "",
          'min="0" step="1"',
        ) +
        input(
          "usable",
          "最终可用数量",
          "number",
          true,
          idx.usable ?? "",
          'min="0" step="1"',
        ) +
        input(
          "retries",
          "重试次数（选填）",
          "number",
          false,
          idx.retries ?? "",
          'min="0" step="1"',
        ) +
        input(
          "minutes",
          "修整分钟（选填）",
          "number",
          false,
          idx.minutes ?? "",
          'min="0" step="1"',
        )
      : ""
  }
  ${input("account","账号（选填）")}${input("plan","套餐（选填）")}${input("cycle_start","周期起始","date")}${input("cycle_end","周期截止","date")}
  ${input("amount", tab === "quota" ? "剩余额度" : tab === "media" ? "批次花费（选填）" : "实付金额", "number", tab !== "media", idx.amount ?? "", 'min="0" step="any"')}
  ${tab === "quota" ? input("unit", "额度单位", "text", true, idx.unit || "", 'placeholder="例如 %、点、美元" maxlength="24"') : input("currency", "币种", "text", true, idx.currency || "CNY", 'maxlength="3" pattern="[A-Za-z]{3}" list="currency-list"')}<datalist id="currency-list">${["CNY", "USD", "JPY", "EUR", "GBP"].map((x) => option(x, x)).join("")}</datalist>
  <div class="form-error" id="form-error" role="alert"></div></div><div class="dialog-foot"><button type="button" id="cancel-note" class="outline">取消</button><button type="submit" class="primary">保存记录</button></div></form>`;
  $("#close-note").onclick = $("#cancel-note").onclick = () => modal.close();
  $("#note-form").onsubmit = async (e) => {
    e.preventDefault();
    const v = Object.fromEntries(new FormData(e.target));
    v.kind = v.kind || kind;
    v.id = uid;
    for (const k of ["produced", "usable", "minutes", "retries"])
      if (k in v) v[k] = v[k] === "" ? null : Number(v[k]);
    try {
      await post("/api/notes", v);
      modal.close();
      toast("记录已保存");
      await refresh();
    } catch (err) {
      $("#form-error").textContent = err.message;
    }
  };
  modal.showModal();
}
