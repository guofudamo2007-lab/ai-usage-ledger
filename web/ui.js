export const $ = (s, root = document) => root.querySelector(s);
export const $$ = (s, root = document) => [...root.querySelectorAll(s)];
export function esc(s) {
  return String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}
const integers = new Intl.NumberFormat("en-US");
const decimals = new Intl.NumberFormat("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
export const full = (n) => n == null ? "未提供" : integers.format(n);
export function amount(n) {
  if (n == null) return ["—", ""];
  for (const [base, suffix] of [
    [1e8, "亿"],
    [1e4, "万"],
  ]) {
    if (n >= base)
      return [
        decimals.format(n / base),
        suffix,
      ];
  }
  return [full(n), ""];
}
export function number(n, cls = "") {
  const [value, unit] = amount(n);
  return `<span class="num ${cls}" title="${full(n)}" aria-label="${full(n)}">${value}<small>${unit}</small></span>`;
}
export function usdParts(n) {
  if (n == null || !Number.isFinite(n)) return null;
  const abs = Math.abs(n);
  const digits = abs > 0 && abs < 0.01 ? 4 : 2;
  const formatted = abs.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
  const dot = formatted.indexOf(".");
  return [
    (n < 0 ? "-" : "") + formatted.slice(0, dot),
    formatted.slice(dot),
  ];
}
export function formatUsd(n) {
  const parts = usdParts(n);
  return parts ? "$" + parts[0] + parts[1] : "—";
}
export function usdHtml(n, cls = "") {
  const parts = usdParts(n);
  if (!parts) return `<span class="num ${cls}">—</span>`;
  return `<span class="usd-num num ${cls}" title="${formatUsd(n)} USD"><b class="usd-sign">$</b><span class="usd-int">${parts[0]}</span><span class="usd-dec">${parts[1]}</span></span>`;
}
export const percent = (a, b) =>
  a == null || !b ? "—" : ((100 * a) / b).toFixed(1) + "%";
const timeFormats = new Map();
export function timeAt(utc, tz, dayOnly = false) {
  if (!utc) return "—";
  const key = tz + (dayOnly ? "/day" : "/time");
  if (timeFormats.has(key)) return timeFormats.get(key).format(new Date(utc));
  const options = {
    timeZone: tz,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  };
  if (!dayOnly)
    Object.assign(options, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });
  const format = new Intl.DateTimeFormat("sv-SE", options);
  timeFormats.set(key, format);
  return format.format(new Date(utc));
}
const glyphs = {
  chart:"chart-line-up", book:"notebook", db:"database", refresh:"arrows-clockwise",
  sliders:"sliders-horizontal", clock:"clock", calendar:"calendar-blank", down:"caret-down",
  right:"caret-right", back:"arrow-left", pulse:"pulse", layers:"stack", info:"info",
  sort:"list-numbers", globe:"globe-hemisphere-east", download:"download-simple", upload:"upload-simple",
  plus:"plus", x:"x", image:"image", wallet:"wallet", grid:"squares-four", pen:"pencil-simple",
  check:"check", plug:"plugs", records:"receipt", arrowIn:"arrow-down-left", arrowOut:"arrow-up-right",
  cache:"memory", order:"sort-descending", link:"link-simple", optic:"cube-transparent",
  ring:"chart-donut", terminal:"terminal-window", jump:"arrow-square-out", wave:"wave-sine"
};
export const icon = (key, size = 16) =>
  `<svg width="${size}" height="${size}" viewBox="0 0 256 256" fill="currentColor" aria-hidden="true"><use href="/vendor/icons.svg#${glyphs[key] || glyphs.chart}"/></svg>`;
export const option = (value, label, selected) =>
  `<option value="${esc(value)}" ${value === selected ? "selected" : ""}>${esc(label)}</option>`;
export function choices(id, label, values, selected, first) {
  const opts = values.map((v) => (Array.isArray(v) ? v : [v, v]));
  return `<label class="field"><span>${esc(label)}</span><select id="${id}" aria-label="${esc(label)}">${first !== undefined ? option("", first, selected) : ""}${opts.map(([v, l]) => option(v, l, selected)).join("")}</select></label>`;
}
let toastTimer;
export function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 5500);
}
export async function api(path, options) {
  const res = await fetch(path, options?.method === "POST" ? options :
    {signal: AbortSignal.timeout(45000), ...options});
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "读取失败，请稍后重试");
  return data;
}
export const post = (path, body) =>
  api(path, {
    method: "POST",
    headers: { "X-Ledger-Local": "1", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
export function empty(title, description, action = "") {
  return `<div class="empty"><span class="empty-icon">${icon("layers", 24)}</span><h3>${esc(title)}</h3><p>${esc(description)}</p>${action}</div>`;
}
export const metNames = {
  total: "总 Token",
  input: "输入（含缓存）",
  output: "输出",
  cache_read: "其中缓存读取",
  cache_write: "其中缓存写入",
  reasoning: "其中推理",
  net: "剔除缓存读取",
  value: "等效 API 价值",
};
export function partial(data, key) {
  return data.n && data[key + "_n"] < data.n
    ? `<span class="coverage">${full(data[key + "_n"])} / ${full(data.n)} 条</span>`
    : "";
}
const vendorInfo = {
  OpenAI: ["openai", "m12 3 7.8 4.5v9L12 21l-7.8-4.5v-9z M8.1 9.8 12 7.5l3.9 2.3v4.4L12 16.5l-3.9-2.3z M12 3v4.5m7.8 9-3.9-2.3M4.2 16.5l3.9-2.3"],
  Google: ["google", "M12 3c1.2 5.2 3.5 7.7 9 9-5.5 1.3-7.8 3.8-9 9-1.2-5.2-3.5-7.7-9-9 5.5-1.3 7.8-3.8 9-9Z"],
  Anthropic: ["anthropic", "M4 18 9.3 5h2.3L17 18M6.4 13h7.7M15.2 5l5.3 13"],
  xAI: ["xai", "M4 18 17 5M8 19 20 7M6 6l10.5 13"],
  "Moonshot AI": ["kimi", "M5 5v14m13-14-8 7 8 7"],
};
export function vendorMark(v) {
  const [cls, path] = vendorInfo[v] || ["misc", "m12 3 9 9-9 9-9-9z M9.5 12h5"];
  return `<span class="vendor-mark ${cls}" aria-hidden="true"><svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linejoin="round" stroke-linecap="round"><path d="${path}"/></svg></span>`;
}
export const scopeText = (s) =>
  !s.first
    ? "全部历史"
    : s.first === s.last
      ? s.first
      : `${s.first} — ${s.last}`;
function cacheReading(data) {
  const complete = data.input_n === data.n && data.cache_read_n === data.n;
  const ratio = complete && data.input > 0 ? data.cache_read / data.input : null;
  const reading = ratio == null ? "未知" : (ratio * 100).toFixed(1) + "%";
  const reason = ratio == null ? (complete ? "输入为零，无法计算占比" : "只有输入和缓存读取均覆盖全部记录时才显示占比") : `${full(data.cache_read)} / ${full(data.input)} 输入 Token`;
  return {ratio,reading,reason};
}
const headlineFrames = new WeakMap();
const usdFrames = new WeakMap();
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
function setTotal(num, target) {
  const previous = headlineFrames.get(num);
  cancelAnimationFrame(previous?.frame);
  const host = num.parentElement;
  const from = previous ? previous.value : host.dataset.total === "" ? null : Number(host.dataset.total);
  host.dataset.total = target ?? "";
  const state = {value: from};
  headlineFrames.set(num, state);
  function paint(value) {
    state.value = value;
    const [digits, unit] = amount(value);
    num.firstChild.nodeValue = digits;
    num.lastElementChild.textContent = unit;
  }
  if (from == null || target == null || !Number.isFinite(from) || from === target || reducedMotion.matches) return paint(target);
  const start = performance.now();
  function tick(now) {
    if (!num.isConnected) return;
    const t = reducedMotion.matches ? 1 : Math.min(1, (now - start) / 300);
    paint(t === 1 ? target : Math.round(target + (from - target) * (1 - t) ** 3));
    if (t < 1) state.frame = requestAnimationFrame(tick);
  }
  state.frame = requestAnimationFrame(tick);
}
function setUsd(band, target) {
  const read = $(".value-read", band);
  if (!read) return;
  const previous = usdFrames.get(band);
  cancelAnimationFrame(previous?.frame);
  const from = previous ? previous.value : band.dataset.usd === "" ? null : Number(band.dataset.usd);
  band.dataset.usd = target ?? "";
  const state = { value: from };
  usdFrames.set(band, state);
  function paint(val) {
    state.value = val;
    if (val == null || !Number.isFinite(val)) {
      read.innerHTML = `<span class="value-empty num">—</span><small class="usd-code">未匹配标价</small>`;
      return;
    }
    const parts = usdParts(val);
    let node = $(".value-main", read);
    if (!node) {
      read.innerHTML = `${usdHtml(val, "value-main")}<small class="usd-code">USD</small>`;
      return;
    }
    $(".usd-int", node).textContent = parts[0];
    $(".usd-dec", node).textContent = parts[1];
    node.title = `${formatUsd(val)} USD`;
  }
  if (from == null || target == null || !Number.isFinite(from) || from === target || reducedMotion.matches) return paint(target);
  const start = performance.now();
  function tick(now) {
    if (!band.isConnected) return;
    const t = reducedMotion.matches ? 1 : Math.min(1, (now - start) / 320);
    paint(t === 1 ? target : target + (from - target) * (1 - t) ** 3);
    if (t < 1) state.frame = requestAnimationFrame(tick);
  }
  state.frame = requestAnimationFrame(tick);
}
function valueMeta(pricing) {
  if (!pricing || !pricing.priced_n) return `<span class="value-note">点击设置单价</span>`;
  if ((pricing.saved ?? 0) >= 0.01) return `<span class="value-saved">缓存约省 ${formatUsd(pricing.saved)}</span>`;
  if (pricing.priced_n < pricing.total_n) return `<span class="value-note">${full(pricing.priced_n)}/${full(pricing.total_n)} 条已定价</span>`;
  return `<span class="value-note">公开标价估算</span>`;
}
function valueBandMarkup(pricing) {
  const total = pricing?.total;
  return `<button type="button" class="value-band" id="show-pricing" data-usd="${total ?? ""}" title="按公开 API 标价估算，仅供参考。点击查看分项或自定义单价。">
    <div class="value-head"><span class="value-label">${icon("wallet", 13)}等效 API 价值</span>${valueMeta(pricing)}</div>
    <div class="value-foot"><div class="value-read">${total == null ? `<span class="value-empty num">—</span><small class="usd-code">未匹配标价</small>` : `${usdHtml(total, "value-main")}<small class="usd-code">USD</small>`}</div>${icon("right", 13)}</div>
  </button>`;
}
export function updateSummary(root, data, pricing) {
  for (const [key,selector] of [["total",".headline-num"],["input",".split-input"],["output",".split-output"],["cache_read",".split-cache_read"]]) {
    const host=$(selector,root), num=$(".num",host);
    const [value,unit]=amount(data[key]);
    if (key === "total") setTotal(num, data.total);
    else {
      num.firstChild.textContent=value;
      $("small",num).textContent=unit;
    }
    num.title=full(data[key]); num.setAttribute("aria-label",num.title);
    const coverage=$(".coverage",host);
    if (data[key+"_n"]===data.n) coverage?.remove();
    else if (coverage) coverage.textContent=`${full(data[key+"_n"])} / ${full(data.n)} 条`;
    else host.insertAdjacentHTML("beforeend",partial(data,key));
  }
  const band = $("#show-pricing", root);
  if (band) {
    const head = $(".value-head", band);
    if (head) head.innerHTML = `<span class="value-label">${icon("wallet", 13)}等效 API 价值</span>${valueMeta(pricing)}`;
    setUsd(band, pricing?.total ?? null);
  }
  $(".hero-base",root).lastChild.textContent=`${full(data.n)} 条实测记录`;
  const {ratio,reading,reason}=cacheReading(data);
  $(".meter-shelf",root).title=reason;
  $(".meter-read strong",root).textContent=reading;
  $(".cache-ruler",root).style.setProperty("--fill", ((ratio ?? 0)*100)+"%");
}
export function summaryCard(data, pricing) {
  const {ratio,reading,reason}=cacheReading(data);
  const metrics = [["input","arrowIn","输入"],["output","arrowOut","输出"],["cache_read","cache","缓存读取"]];
  return `<section class="hero"><article class="hero-total">
    <div class="measure-label"><span class="measure-icon">${icon("layers",18)}</span>当期总用量</div>
    <div class="headline-num" data-total="${data.total ?? ""}">${number(data.total)} ${partial(data,"total")}</div>
    <p class="token-comparison" hidden></p>
    <div class="hero-base"><span class="active-days"></span><i></i>${full(data.n)} 条实测记录</div>
    ${valueBandMarkup(pricing)}
    <div class="meter-shelf" title="${esc(reason)}"><div class="meter-read"><span>缓存占输入</span><strong class="num">${reading}</strong></div><div class="cache-ruler" aria-hidden="true" style="--fill:${(ratio ?? 0)*100}%"><i></i></div></div>
    <button id="show-breakdown" aria-haspopup="dialog" title="查看完整用量拆分">${icon("jump",15)}<span>完整拆分</span></button></article>
    <article class="trend-card"><div class="trend-title"><h2>每日趋势</h2><div class="legend"><span><i class="total-key"></i>总量</span><span><i class="cache-key"></i>缓存读取</span></div></div>
    <div id="plot" role="group" aria-label="每日用量趋势，左右方向键查看各日明细" tabindex="0"></div>
    <div class="split">${metrics.map(([key,glyph,label])=>`<div class="split-item split-${key}"><div class="metric-label" title="${metNames[key]}">${icon(glyph,14)}${label}</div>${number(data[key])}${partial(data,key)}</div>`).join("")}</div></article></section>`;
}
// A given entry retains its colour when its ranking changes or other entries are filtered.
const entryColors = {
  agy:"#83a9de", "agy-cli":"#a2bdde", "agy-ide":"#929acc", codex:"#84dcbf",
  "openai-api":"#99cabe", "gemini-api":"#8db2e0", "ai-studio":"#8ca7bc",
  "claude-api":"#d4b19f", "grok-api":"#b3bac9", "kimi-api":"#b8a8d2",
};
const entryColor = key => entryColors[key] || "#97a7ab";
export function entryPanel(data, names) {
  const rows = [...data.rankings.entry].sort((a,b)=>(b.total??-1)-(a.total??-1) || a.entry.localeCompare(b.entry));
  let offset = 0;
  const segments = rows.filter(row=>row.total>0).length;
  const arcs = rows.map(row=>{
    if (!row.total || !data.totals.total) return "";
    const length = 100 * row.total / data.totals.total;
    const gap = segments === 1 ? 0 : Math.min(1.35,length*.12);
    const point = fraction => {
      const angle = fraction * Math.PI / 50;
      return `${68+53*Math.cos(angle)} ${68+53*Math.sin(angle)}`;
    };
    const path = `M${point(offset+gap/2)} A53 53 0 0 1 ${point(offset+length/2)} A53 53 0 0 1 ${point(offset+length-gap/2)}`;
    const arc = `<path data-arc="${esc(row.entry)}" d="${path}" stroke="${entryColor(row.entry)}"><title>${esc(names[row.entry] || row.entry)} · ${percent(row.total,data.totals.total)}</title></path>`;
    offset+=length;
    return arc;
  });
  const netCoverage = partial(data.totals,"total");
  return `<aside class="entry-card"><div class="section-head"><div class="title-line"><h2>入口分布</h2></div><span class="entry-head-icon">${icon("ring",20)}</span></div>
    <div class="donut"><svg viewBox="0 0 136 136" fill="none" aria-hidden="true"><circle class="dial-ticks" cx="68" cy="68" r="64" pathLength="100" stroke-dasharray=".25 1.75"/><g stroke-width="9" transform="rotate(-90 68 68)"><circle cx="68" cy="68" r="53" stroke="var(--line-soft)"/>${arcs.join("")}</g></svg>
    <span><b class="num">${rows.length}</b><small>使用入口</small></span></div>
    <div class="entry-list">${rows.map(row=>`<button data-entry-filter="${esc(row.entry)}" title="筛选 ${esc(names[row.entry] || row.entry)} · ${full(row.total)} Token"><i style="--entry:${entryColor(row.entry)}"></i><span>${esc(names[row.entry] || row.entry)}</span><b class="num">${percent(row.total,data.totals.total)}</b>${icon("right",12)}</button>`).join("")}</div>${netCoverage ? `<div class="ring-note">已知总量覆盖 ${netCoverage}</div>` : ""}</aside>`;
}
