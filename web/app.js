import {
  $,
  $$,
  api,
  post,
  esc,
  icon,
  number,
  full,
  percent,
  choices,
  option,
  toast,
  empty,
  metNames,
  partial,
  vendorMark,
  scopeText,
  summaryCard,
  updateSummary,
  entryPanel,
  timeAt,
} from "./ui.js";
import { mountSelects, disposeSelects, mountTrend } from "./instruments.js";
import { renderSources, renderBook } from "./books.js";

const main = $("#main"),
  detected = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
const defaultState = {
  tab: "home",
  period: "week",
  tz: localStorage.getItem("meter-zone") || detected,
  group: "model",
  sort: "total",
};
const readState = () => ({
  ...defaultState,
  ...Object.fromEntries(new URLSearchParams(location.search)),
});
let state = readState(),
  registry = { entries: {}, values: {} },
  serial = 0,
  timer,
  filtersOpen = null;
let detailFilters = false,
  expanded = new Set(),
  job = {},
  lastFinished,
  chartObserver;
let shown = null, shownKey = "", knownRevision = null, lastDay = "", bookRows = null;
const views = new Map();
const pendingViews = new Map();
// Keep a few bounded, already rendered rankings for repeated sort and scope switches.
const rankViews = [];
function fetchView(key, query) {
  let request = pendingViews.get(key);
  if (!request) {
    request = api("/api/view?" + query).finally(() => pendingViews.delete(key));
    pendingViews.set(key, request);
  }
  return request;
}
const periods = [["today","今天"],["week","近 7 天"],["month","本月"],["all","全部历史"],["custom","自定义"]];
function segments(key, items, active, label, cls) {
  const idx = Math.max(0, items.findIndex(([v]) => v === active));
  return `<div class="switch ${cls}" data-switch="${key}" role="group" aria-label="${label}" style="--count:${items.length};--index:${idx}">${items.map(([v,name]) =>
    `<button data-${key}="${v}" aria-pressed="${v === active}" class="${v === active ? "selected" : ""}">${name}</button>`).join("")}</div>`;
}
function selectSegment(key, value) {
  const root = $(`[data-switch="${key}"]`);
  if (!root) return;
  for (const [i, b] of $$("button", root).entries()) {
    const active = b.dataset[key] === value;
    b.setAttribute("aria-pressed", String(active));
    b.classList.toggle("selected", active);
    if (active) root.style.setProperty("--index", i);
  }
}
function sortedRows(rows, metric, keys) {
  return [...rows].sort((a,b) => {
    if ((a[metric] == null) !== (b[metric] == null)) return a[metric] == null ? 1 : -1;
    if (a[metric] !== b[metric]) return b[metric] - a[metric];
    for (const key of keys) {
      if (a[key] !== b[key]) return a[key] < b[key] ? -1 : 1;
    }
    return 0;
  });
}
function queryKey() {
  const args = effective();
  delete args.group;
  delete args.sort;
  if (args.period !== "custom") { delete args.from; delete args.to; }
  if (!args.detail) { delete args.page; args.rankings = "1"; }
  const q = params(args);
  q.sort();
  const day = ["today","week","month"].includes(args.period) ? timeAt(new Date(), args.tz, true) : "";
  return {query:q.toString(), key:day + "|" + q.toString()};
}
function revised(revision) {
  if (revision == null || (knownRevision != null && revision <= knownRevision)) return false;
  views.clear();
  rankViews.length = 0;
  shownKey = "";
  knownRevision = revision;
  return true;
}
const validTabs = ["home", "sources", "book"];
const params = (obj) =>
  new URLSearchParams(
    Object.entries(obj).filter(([, v]) => v !== "" && v != null),
  );
const zones = [
  ...new Set([
    detected,
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Hong_Kong",
    "Asia/Singapore",
    "UTC",
    "Europe/London",
    "Europe/Berlin",
    "America/New_York",
    "America/Los_Angeles",
    "Australia/Sydney",
    ...(Intl.supportedValuesOf?.("timeZone") || []),
  ]),
];
$("#modal").addEventListener("click", (e) => {
  if (e.target === e.currentTarget) e.currentTarget.close();
});
for (const el of $$("[data-icon]")) el.innerHTML = icon(el.dataset.icon, 17);
function navigate(patch, { push = false, top = false } = {}) {
  state = { ...state, ...patch };
  for (const [k, v] of Object.entries(state))
    if (v === "" || v == null) delete state[k];
  const marker = push ? { fromOverview: !!patch.dm } : history.state || {};
  history[push ? "pushState" : "replaceState"](
    marker,
    "",
    location.pathname + "?" + params(state),
  );
  if (top) window.scrollTo(0, 0);
  if (state.tab === "home" && !state.dm && shown && $("#rank") && !main.matches('[aria-busy="true"]') &&
      Object.keys(patch).every(k => k === "sort" || k === "group") && queryKey().key === shownKey) {
    renderRank(shown);
    $(".page-head .export").outerHTML = exportButtons();
    return;
  }
  render();
}
const clearDetail = {
  dm: null,
  dv: null,
  de: null,
  dp: null,
  ds: null,
  page: null,
};
function tab(name) {
  navigate({ tab: name, ...clearDetail }, { push: true, top: true });
}
for (const el of $$("[data-nav]")) el.onclick = () => tab(el.dataset.nav);
$(".brand").onclick = (e) => {
  e.preventDefault();
  tab("home");
};
window.onpopstate = () => {
  state = readState();
  render();
};
function effective() {
  const a = { ...state };
  a.tz = a.tz || detected;
  if (state.dm)
    Object.assign(a, {
      model: state.dm,
      vendor: state.dv,
      detail: "1",
      group: "model",
      entry: state.entry || state.de,
      project: state.project || state.dp,
      source: state.source || state.ds,
    });
  for (const k of ["dm", "dv", "de", "dp", "ds", "tab", "booktab"]) delete a[k];
  return a;
}
function selectFilter(id, label, field, value, blank) {
  const values = registry.values[field] || [],
    list =
      field === "entry" ? values.map((v) => [v, registry.entries[v]]) : values;
  return choices(id, label, list, value || "", blank);
}
function filters(data) {
  const count = ["vendor", "model", "entry", "project", "source"].filter(
    (k) => state[k],
  ).length;
  const dates =
    state.period === "custom"
      ? `<form id="dates" class="dates"><label>起始 <input type="date" name="from" required value="${esc(state.from || data.scope.first || "")}"></label><span>—</span><label>截止 <input type="date" name="to" required value="${esc(state.to || data.scope.last || "")}"></label><button class="small-btn">应用日期</button></form>`
      : "";
  const activeFilters = filtersOpen ?? count > 0;
  return `<div class="filter-line">${segments("period",periods,state.period,"时间范围","periods")}
  <div class="filter-tools"><label class="zone" title="统计时区">${icon("globe")}<select id="timezone" aria-label="统计时区">${[...new Set([...zones, state.tz])].map((v) => option(v, v, state.tz)).join("")}</select></label>
  <button class="filter-button ${activeFilters ? "active" : ""}" id="toggle-filters" aria-expanded="${activeFilters}">${icon("sliders")}筛选${count ? `<b>${count}</b>` : ""}</button></div></div><div class="filter-extra">${dates}
  ${
    activeFilters
      ? `<div class="filter-fields">${[
          ["vendor", "模型厂商"],
          ["model", "模型"],
          ["entry", "使用入口"],
          ["project", "项目"],
          ["source", "数据来源"],
        ]
          .filter(([k]) => !state.dm || !["vendor", "model"].includes(k))
          .map(([k, label]) =>
            selectFilter("f-" + k, label, k, state[k], "全部"),
          )
          .join("")}<button class="text-btn" id="reset">清除筛选</button></div>`
      : ""
  }</div>`;
}
function bindFilters(data) {
  for (const el of $$("[data-period]"))
    el.onclick = () => {
      const next = { period: el.dataset.period, page: null };
      if (next.period === "custom" && (!state.from || !state.to)) {
        next.from = data.scope.first || timeAt(new Date(), state.tz, true);
        next.to = data.scope.last || next.from;
      }
      navigate(next);
    };
  $("#timezone").onchange = (e) => {
    localStorage.setItem("meter-zone", e.target.value);
    navigate({ tz: e.target.value, page: null });
  };
  $("#toggle-filters").onclick = () => {
    filtersOpen = !$(".filter-fields");
    drawFilters(data, true);
  };
  const dateForm = $("#dates");
  if (dateForm)
    dateForm.onsubmit = (e) => {
      e.preventDefault();
      const d = Object.fromEntries(new FormData(dateForm));
      if (d.from > d.to) {
        toast("起始日期须早于或等于截止日期");
        return;
      }
      navigate({ ...d, page: null });
    };
  for (const el of $$('select[id^="f-"]'))
    el.onchange = () => navigate({ [el.id.slice(2)]: el.value, page: null });
  if ($("#reset"))
    $("#reset").onclick = () => {
      filtersOpen = false;
      navigate({
        vendor: null,
        model: null,
        entry: null,
        project: null,
        source: null,
        page: null,
      });
    };
}
let filterSignature = "";
function drawFilters(data, force = false) {
  const root = $("#scope-controls");
  const signature = JSON.stringify([state.period === "custom",state.from,state.to,state.tz,
    !!state.dm,filtersOpen,state.vendor,state.model,state.entry,state.project,state.source,registry.version]);
  if (force || !root.firstChild || signature !== filterSignature) {
    disposeSelects(root);
    root.innerHTML = filters(data);
    filterSignature = signature;
    bindFilters(data);
    mountSelects(root);
  } else {
    selectSegment("period", state.period);
    bindFilters(data);
  }
}
function pageHead(title, desc, actions = "") {
  return `<div class="page-head"><div><h1>${esc(title)}</h1><p>${desc}</p></div><div class="head-actions">${actions}</div></div>`;
}
const syncButton = () =>
  `<button class="outline" id="sync">${icon("refresh")}<span>更新用量</span></button>`;
async function doSync() {
  try {
    job = await post("/api/sync", {});
    updateJob();
    schedule(1000);
  } catch (err) {
    toast(err.message);
  }
}
function updateJob() {
  const el = $("#sync");
  if (el) {
    el.disabled = !!job.running;
    $("span", el).textContent = job.running ? "正在采集" : "更新用量";
  }
  const detail = $("#sync-status");
  if (detail) {
    detail.textContent = job.running
      ? `正在读取 ${registry.entries[job.entry] || "本机来源"} · ${job.done || 0} / ${job.files || 0} 份`
      : job.error || "打开账本时自动更新，运行期间每 5 分钟检查一次。";
  }
}
function schedule(ms = 15000) {
  clearTimeout(timer);
  timer = setTimeout(poll, ms);
}
async function poll() {
  try {
    job = await api("/api/status");
    const changed = revised(job.revision);
    const finished = lastFinished !== undefined && job.finished !== lastFinished;
    lastFinished = job.finished;
    const day = timeAt(new Date(), state.tz, true);
    const rollover = lastDay && lastDay !== day;
    lastDay = day;
    if (changed) await loadOptions();
    if (!$("dialog").open && ((state.tab === "home" && (changed || rollover || (shown && !shownKey))) ||
        (state.tab === "sources" && (changed || finished)))) await render();
    updateJob();
  } catch {
  } finally {
    schedule(job.running ? 1800 : 15000);
  }
}
let optionsVersion = 0;
async function loadOptions() {
  registry = await api("/api/options");
  registry.version = ++optionsVersion;
}
const groups = [
  ["model", "模型"],
  ["vendor", "厂商"],
  ["entry", "入口"],
];
const sorts = [
  ["total", "总 Token"],
  ["output", "输出 Token"],
  ["net", "剔除缓存读取"],
];
const groupLabels = { model: "模型", vendor: "厂商", entry: "入口" };
function entryRows(row, metric) {
  return `<div class="subrows">${sortedRows(row.children,metric,["entry"]).map(child =>
    `<div><span>${icon("link",13)}${esc(registry.entries[child.entry])}</span><b class="num" title="${full(child[metric])}">${full(child[metric])}</b><small>${percent(child[metric],row[metric])}</small></div>`).join("")}</div>`;
}
function rankMarkup(data) {
  const metric = state.sort,
    group = state.group,
    max = data.rank[0]?.[metric] || 0;
  const coverage =
    metric === "net" || data.totals[metric + "_n"] < data.totals.n;
  const bar = (value) =>
    value == null
      ? ""
      : `<div class="bar-track"><i style="width:${max ? (100 * value) / max : 0}%"></i></div>`;
  const rows = data.rank
    .map((r, i) => {
      const name = group === "entry" ? registry.entries[r.entry] : r[group];
      const open = expanded.has(JSON.stringify([r.vendor, r.model]));
      const sub =
        group === "model"
          ? r.vendor
          : group === "entry"
            ? "使用入口"
            : "模型厂商";
      return `<div class="rank-row" data-index="${i}"><div class="row-no">${String(i + 1).padStart(2, "0")}</div>
    <div class="identity">${group === "entry" ? `<span class="vendor-mark misc">${icon("plug")}</span>` : vendorMark(r.vendor)}<div><button class="name-btn" data-item="${i}">${esc(name)}</button><span class="row-sub">${esc(sub)}</span></div></div>
    <div class="rank-bar">${bar(r[metric])}</div><div class="rank-qty">${number(r[metric])}</div><div class="rank-share">${percent(r[metric], data.totals[metric])}</div>
    ${group === "model" ? `<button class="expand" data-expand="${i}" title="${open ? "收起" : "展开"}入口分布" aria-label="${open ? "收起" : "展开"} ${esc(name)} 的入口" aria-expanded="${open}">${icon("right", 15)}</button>` : `<button class="expand" data-item="${i}" aria-label="查看该分组">${icon("right", 15)}</button>`}</div>
    ${group === "model" && open ? entryRows(r, metric) : ""}`;
    })
    .join("");
  return `  <div class="rank-table"><div class="rank-header"><span>#</span><span>${groupLabels[group]}名称</span><span>相对用量</span><span>${metNames[metric]}</span><span>占比</span><span></span></div>${rows}</div>
  <div class="rank-foot">${icon("info", 14)}${coverage ? `此排序覆盖 ${full(data.totals[metric + "_n"])} / ${full(data.totals.n)} 条实测记录，占比按已知用量计算。` : group === "model" ? "点击模型名称查看明细，行末箭头展开入口分布。" : "点击名称筛选该分组的模型。"}</div>`;
}
function renderRank(data) {
  const el = $("#rank");
  if (!el) return;
  const group = state.group, metric = state.sort;
  if (!el.firstChild) {
    el.innerHTML = `<div class="section-head"><div class="title-line"><h2>使用排行</h2><span class="section-count"></span></div>
      <div class="rank-controls">${segments("group",groups,group,"排行分组","group-switch")}<label class="sort-control">${icon("order", 15)}<select id="sort" aria-label="排行排序">${sorts.map(v=>option(...v,metric)).join("")}</select></label></div></div><div class="rank-body"></div>`;
    for (const b of $$("[data-group]", el)) b.onclick = () => navigate({group:b.dataset.group});
    $("#sort").onchange = (e) => navigate({sort:e.target.value});
    mountSelects($(".rank-controls",el));
  }
  selectSegment("group", group);
  const picker = $("#sort").tomselect;
  if (picker.getValue() !== metric) picker.setValue(metric,true);
  const source = data.rankings[group], active = $(".rank-body",el);
  const found = rankViews.findIndex(v=>v.source===source && v.metric===metric && v.version===registry.version);
  let body;
  if (found>=0) {
    const [saved]=rankViews.splice(found,1);
    rankViews.push(saved);
    body=saved.body;
  } else {
    const rank=sortedRows(source,metric,group==="model"?["vendor","model"]:[group]);
    body=document.createElement("div");
    body.className="rank-body";
    body.innerHTML=rankMarkup({...data,rank});
    body.onclick=e=>{
      const button=e.target.closest("[data-item],[data-expand]");
      if (!button) return;
      const row=rank[Number(button.dataset.item ?? button.dataset.expand)];
      if (button.hasAttribute("data-expand")) {
        const key=JSON.stringify([row.vendor,row.model]), open=!expanded.has(key);
        const line=button.closest(".rank-row");
        if (open) {
          expanded.add(key);
          line.insertAdjacentHTML("afterend",entryRows(row,metric));
        } else {
          expanded.delete(key);
          if (line.nextElementSibling?.matches(".subrows")) line.nextElementSibling.remove();
        }
        rankViews.length=0;
        button.setAttribute("aria-expanded",String(open));
        button.setAttribute("aria-label",(open?"收起 ":"展开 ")+row.model+" 的入口");
        button.title=(open?"收起":"展开")+"入口分布";
      } else if (group==="model") {
        detailFilters=false;
        navigate({dm:row.model,dv:row.vendor,page:null,de:null,dp:null,ds:null},{push:true,top:true});
      } else {
        navigate({[group]:row[group],group:"model"},{push:true,top:true});
      }
    };
    if (rank.length<=128) {
      rankViews.push({source,metric,body,version:registry.version});
      if (rankViews.length>5) rankViews.shift();
    }
  }
  if (active!==body) active.replaceWith(body);
  $(".section-count",el).textContent=source.length+" 个"+groupLabels[group];
}
function exportButtons() {
  const base = params(effective());
  return `<div class="export"><span>${icon("download", 15)}导出</span><a href="/api/export?${esc(base.toString())}&amp;format=csv" download>CSV</a><a href="/api/export?${esc(base.toString())}&amp;format=json" download>JSON</a></div>`;
}
function distribution(data, key, title) {
  return `<section class="distribution"><h3>${title}</h3><div>${
    data[key]
      .map((r) => {
        const name = key === "entry" ? registry.entries[r[key]] : r[key];
        return `<button data-dist="${key}" data-value="${esc(r[key])}" title="筛选 ${esc(name)}"><span>${esc(name)}</span>${number(r.total)}<i style="width:${data.totals.total ? (100 * r.total) / data.totals.total : 0}%"></i></button>`;
      })
      .join("") || '<p class="muted">当前范围无记录</p>'
  }</div></section>`;
}
function details(data) {
  const filters = detailFilters || state.de || state.dp || state.ds;
  const keys = [
    "total",
    "input",
    "output",
    "cache_read",
    "cache_write",
    "reasoning",
    "net",
  ];
  const pages = Math.max(1, Math.ceil(data.totals.n / data.limit));
  return `<div class="distributions">${distribution(data, "entry", "入口分布")}${distribution(data, "project", "项目分布")}</div>
  <section class="records"><div class="section-head"><div class="title-line"><h2>逐条记录</h2><span class="section-count">${full(data.totals.n)} 条</span></div><div class="head-actions"><button class="outline" id="detail-filter">${icon("sliders")}明细筛选</button>${exportButtons()}</div></div>
  ${
    filters
      ? `<div class="detail-fields filter-fields">${[
          ["entry", "de", "入口"],
          ["project", "dp", "项目"],
          ["source", "ds", "数据来源"],
        ]
          .map(([field, k, label]) =>
            state[field]
              ? `<p class="muted">${label} · ${esc(field === "entry" ? registry.entries[state[field]] : state[field])}</p>`
              : selectFilter("d-" + k, label, field, state[k], "全部"),
          )
          .join(
            "",
          )}<button id="dreset" class="text-btn">清除明细筛选</button></div>`
      : ""
  }
  <div class="scroll-table"><table><thead><tr>${["时间", "平台 / 入口", "模型 / 项目", ...keys.map((k) => metNames[k]), "数据来源"].map((s) => `<th>${s}</th>`).join("")}</tr></thead><tbody>${data.records.map((r) => `<tr><td class="num">${esc(timeAt(r.timestamp, state.tz))}</td><td>${esc(r.platform)}<small>${esc(r.entry_name)}</small></td><td>${esc(r.model)}<small>${esc(r.project)}</small></td>${keys.map((k) => `<td class="num" title="${full(r[k])}">${r[k] == null ? "—" : full(r[k])}</td>`).join("")}<td>${esc(r.source)}</td></tr>`).join("")}</tbody></table></div>
  <div class="pager"><span>“—” 表示来源未提供该项</span><button class="small-btn" id="prev" ${data.page === 0 ? "disabled" : ""}>上一页</button><span class="num">${data.page + 1} / ${pages}</span><button class="small-btn" id="next" ${data.page + 1 >= pages ? "disabled" : ""}>下一页</button></div></section>`;
}
function draw(data) {
  const detail = !!state.dm,
    active = state.entry ? registry.entries[state.entry] : null;
  const reuseOverview = !detail && data.totals.n > 0 && $("#rank") && chartObserver;
  const description = `<span class="period-caption">${icon("calendar", 14)}${esc(scopeText(data.scope))}</span>${active ? `<span class="dot-divider">·</span>${esc(active)}` : ""}`;
  const controls = detail ? exportButtons() : exportButtons() + syncButton();
  if (!$("#view-body")) main.innerHTML = '<div id="view-head"></div><section id="scope-controls" class="filter-sticky" aria-label="统计范围"></section><div id="view-body"></div>';
  $("#view-head").innerHTML = (detail ? `<button class="back text-btn" id="back">${icon("back",15)}返回总览</button>` : "") + pageHead(
      detail ? state.dm : "用量总览",
      detail ? esc(state.dv) + ' <span class="dot-divider">/</span> ' + description : description,
      controls);
  drawFilters(data);
  if (reuseOverview) {
    updateSummary($(".hero"),data.totals);
    $(".entry-card").outerHTML = entryPanel(data,registry.entries);
  } else {
    chartObserver?.disconnect();
    chartObserver=null;
    disposeSelects($("#view-body"));
    $("#view-body").innerHTML =
      (data.totals.n
        ? summaryCard(data.totals)
        : empty(
            "当前范围没有实测记录",
            "可调整时间与筛选，或在数据接入页更新本机来源、导入逐请求文件。",
            '<button id="go-sources" class="primary">查看数据接入</button>',
          )) +
      (detail
        ? details(data)
        : data.totals.n
          ? `<div class="overview-grid"><section id="rank" class="rank"></section>${entryPanel(data,registry.entries)}</div>`
          : "");
  }
  const breakdown = $("#show-breakdown");
  if (breakdown)
    breakdown.onclick = () => {
      const totals = data.totals,
        modal = $("#modal");
      const rows = [
        "total",
        "input",
        "cache_read",
        "cache_write",
        "output",
        "reasoning",
        "net",
      ]
        .map(
          (key) =>
            `<tr data-field="${key}"><td>${metNames[key]}</td><td class="num">${full(totals[key])}</td><td class="num">${full(totals[key + "_n"])} / ${full(totals.n)}</td></tr>`,
        )
        .join("");
      $("#modal-content").innerHTML =
        `<div class="dialog-head"><h2>用量拆分</h2><button id="close-breakdown" aria-label="关闭">${icon("x")}</button></div>
      <div class="scroll-table"><table><thead><tr><th>统计项</th><th>完整 Token 数</th><th>已知 / 全部记录</th></tr></thead><tbody>${rows}</tbody></table></div>
      <p class="breakdown-note">缓存读写包含在输入中，推理包含在输出中。每项仅合计有可靠数值的记录。</p>`;
      $("#close-breakdown").onclick = () => modal.close();
      modal.showModal();
    };
  if ($("#sync")) $("#sync").onclick = doSync;
  if ($("#back"))
    $("#back").onclick = () =>
      history.state?.fromOverview
        ? history.back()
        : navigate({ ...clearDetail }, { top: true });
  if ($("#go-sources")) $("#go-sources").onclick = () => tab("sources");
  if (!detail) {
    renderRank(data);
    for (const button of $$("[data-entry-filter]")) button.onclick = () => navigate({entry:button.dataset.entryFilter,page:null},{push:true,top:true});
  } else {
    mountSelects($("#view-body"));
    $("#detail-filter").onclick = () => {
      detailFilters = !$(".detail-fields");
      draw(data);
    };
    if ($("#dreset"))
      $("#dreset").onclick = () =>
        navigate({ de: null, dp: null, ds: null, page: null });
    for (const el of $$('select[id^="d-"]'))
      el.onchange = () => navigate({ [el.id.slice(2)]: el.value, page: null });
    for (const el of $$("[data-dist]"))
      el.onclick = () => {
        detailFilters = true;
        navigate({
          [el.dataset.dist === "entry" ? "de" : "dp"]: el.dataset.value,
          page: null,
        });
      };
    $("#prev").onclick = () => navigate({ page: String(data.page - 1) });
    $("#next").onclick = () => navigate({ page: String(data.page + 1) });
  }
  const plot = $("#plot");
  if (reuseOverview) chartObserver.update(data.trend,data.scope);
  else if (plot) chartObserver=mountTrend(plot,data.trend,data.scope);
  updateJob();
}
async function render() {
  const version = ++serial,
    t = validTabs.includes(state.tab) ? state.tab : "home";
  for (const el of $$("[data-nav]")) {
    el.classList.toggle("current", el.dataset.nav === t);
    if (el.dataset.nav === t) el.parentElement.style.setProperty("--index", [...el.parentElement.children].indexOf(el));
    el.setAttribute("aria-current", el.dataset.nav === t ? "page" : "false");
  }
  selectSegment("period", state.period);
  if (t !== "book") bookRows = null;
  try {
    if (t === "home") {
      if (!groups.some(([v]) => v === state.group)) state.group = "model";
      if (!sorts.some(([v]) => v === state.sort)) state.sort = "total";
      const {query, key} = queryKey();
      let data = views.get(key);
      if (!data) {
        main.setAttribute("aria-busy","true");
        if ($("#view-body")) $("#view-body").inert = true;
        if ($(".page-head .export")) $(".page-head .export").inert = true;
        data = await fetchView(key, query);
        if (serial !== version) return;
        if (knownRevision != null && data.revision < knownRevision) return render();
        if (revised(data.revision)) {
          await loadOptions();
          if (serial !== version) return;
        }
      }
      views.delete(key);
      views.set(key, data);
      if (views.size > 8) views.delete(views.keys().next().value);
      shown = data;
      shownKey = key;
      draw(data);
      $("#view-body").inert = false;
    } else if (t === "sources") {
      const rows = await api("/api/sources");
      if (serial !== version) return;
      chartObserver?.disconnect();
      chartObserver = null;
      disposeSelects(main);
      renderSources(main, rows, {
        tz: state.tz,
        pageHead,
        syncButton,
        doSync,
        refresh: async () => {
          views.clear();
          shownKey = "";
          await loadOptions();
          await render();
        },
      });
      updateJob();
    } else {
      const rows = bookRows || await api("/api/notes");
      if (serial !== version) return;
      chartObserver?.disconnect();
      chartObserver = null;
      disposeSelects(main);
      bookRows = rows;
      renderBook(main, rows, {
        registry,
        pageHead,
        tz: state.tz,
        booktab: state.booktab || "media",
        navigate,
        refresh: () => { bookRows = null; return render(); },
      });
    }
  } catch (err) {
    if (serial === version) {
      chartObserver?.disconnect();
      chartObserver = null;
      disposeSelects(main);
      main.innerHTML = empty(
        "未能读取这个视图",
        err.message,
        '<button class="primary" id="retry">回到默认总览</button>',
      );
      $("#retry").onclick = () => {
        state = { ...defaultState };
        navigate({});
      };
    }
  } finally {
    if (serial === version) main.removeAttribute("aria-busy");
  }
}
async function start() {
  try {
    const [,status] = await Promise.all([loadOptions(),api("/api/status")]);
    knownRevision = status.revision;
    lastFinished = status.finished;
    job = status;
  } catch (err) {
    toast(err.message);
  }
  await render();
  lastDay = timeAt(new Date(), state.tz || detected, true);
  schedule(job.running ? 1800 : 15000);
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) schedule(50); });
start();
