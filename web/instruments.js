import { $, $$, amount, esc, formatUsd, full, metNames } from "./ui.js";

export function mountSelects(root) {
  for (const select of $$("select", root)) {
    if (select.tomselect) continue;
    const searchable = select.options.length > 8;
    const control = new TomSelect(select, {
      allowEmptyOption: true, hideSelected: false, create: false, maxOptions: 90,
      controlInput: searchable ? '<input type="text" autocomplete="off" size="1">' : null,
      render: {
        no_results: () => '<div class="no-results">没有匹配项</div>',
        option: (item, escape) => `<div><span>${escape(item.text)}</span></div>`,
      },
    });
    control.focus_node.setAttribute("aria-label", select.getAttribute("aria-label") || "");
    if (searchable) {
      control.on("dropdown_open", () => control.control_input.placeholder = "输入关键词…");
      control.on("dropdown_close", () => control.control_input.placeholder = "");
    }
  }
}
export function disposeSelects(root) {
  if (root) for (const select of $$("select", root)) select.tomselect?.destroy();
}

const DAY = 86400000;
const dayNumber = date => Math.floor(Date.parse(date + "T00:00:00Z") / DAY);
const dateString = day => new Date(day * DAY).toISOString().slice(0, 10);
const tickValue = n => {
  const [v, unit] = amount(n);
  return v.replace(/\.00$/, "").replace(/(\.\d)0$/, "$1") + unit;
};
function roundedTop(value) {
  if (!(value > 0)) return 1;
  const base = 10 ** Math.floor(Math.log10(value));
  const step = [1,1.5,2,2.5,3,4,5,6,8,10].find(n => n * base >= value);
  return (step || 10) * base;
}
function samples(rows,first,last) {
  const x = [], total = [], cache = [];
  for (const row of rows) {
    const day = dayNumber(row.day);
    if (x.length && day - x.at(-1) > 1) {
      x.push(x.at(-1)+1); total.push(null); cache.push(null);
    }
    x.push(day); total.push(row.total); cache.push(row.cache_read);
  }
  if (x[0] > first) { x.unshift(x[0]-1); total.unshift(null); cache.unshift(null); }
  if (x.at(-1) < last) { x.push(x.at(-1)+1); total.push(null); cache.push(null); }
  return [x,total,cache];
}
export function mountTrend(el, rows, scope) {
  if (!rows.length) return null;
  let first, last, days, byDay, peak, top, series, isolated, hasData;
  function setRange(records,range) {
    first=dayNumber(range.first || records[0].day);
    last=dayNumber(range.last || records.at(-1).day);
    days=last-first+1;
    byDay=new Map(); peak=0; hasData=false;
    for (const row of records) {
      byDay.set(dayNumber(row.day),row);
      if (row.total!=null || row.cache_read!=null) hasData=true;
      peak=Math.max(peak,row.total ?? 0,row.cache_read ?? 0);
    }
    top=roundedTop(peak);
    series=samples(records,first,last);
    isolated=[];
    const [x,,cache]=series;
    for (let i=0;i<x.length;i++) {
      if (cache[i]!=null && cache[i-1]==null && cache[i+1]==null) isolated.push(i);
    }
  }
  setRange(rows,scope);
  const host = document.createElement("div"), tip = document.createElement("div");
  host.className = "chart-surface"; tip.className = "chart-tip"; tip.hidden = true;
  tip.setAttribute("role","status");
  const missing = document.createElement("div");
  missing.className="plot-missing"; missing.textContent="来源未提供总量与缓存读取"; missing.hidden=hasData;
  el.replaceChildren(host,missing,tip);
  let width = el.clientWidth, height = el.clientHeight, selected = null, gradient, gradientTop, gradientHeight;
  const css=getComputedStyle(document.documentElement);
  const color=name=>css.getPropertyValue("--"+name).trim();
  const chart = new uPlot({
    width, height, padding:[8,10,0,0],
    scales:{ x:{time:false,range:()=>[first-.5,last+.5]}, y:{range:()=>[0,top]} },
    legend:{show:false}, select:{show:false},
    cursor:{drag:{x:false,y:false,setScale:false}, y:false, points:{show:false}},
    axes:[
      {size:22,side:2,stroke:()=>color("muted"),font:"10px 'Segoe UI',sans-serif",grid:{show:false},ticks:{show:false},
        splits:(u)=>{
          const count = Math.min(days,Math.max(2,Math.floor((u.bbox.width/u.pxRatio || width)/66)));
          if (count === 1) return [first];
          return Array.from({length:count},(_,i)=>first+Math.round(i*(days-1)/(count-1)));
        }, values:(_,v)=>v.map(x=>dateString(x).slice(days>700?0:5,days>700?7:10).replace("-","/"))},
      {size:48,side:3,stroke:()=>color("muted"),font:"10px 'Segoe UI',sans-serif",gap:7,
        splits:()=>peak<=1?[0,1]:[0,top/2,top].filter(Number.isInteger),
        values:(_,v)=>v.map(tickValue),ticks:{show:false},grid:{stroke:()=>color("line-soft"),dash:[3,5],width:1}},
    ],
    series:[{}, {
      label:"总量",stroke:()=>color("chart-ink"),width:0,
      fill:u=>{
        const box = u.bbox;
        if (!gradient || gradientTop!==box.top || gradientHeight!==box.height) {
          gradientTop=box.top; gradientHeight=box.height;
          gradient=u.ctx.createLinearGradient(0,box.top,0,box.top+Math.max(1,box.height));
          gradient.addColorStop(0,color("chart-ink")); gradient.addColorStop(1,color("mark"));
        }
        return gradient;
      },
      paths:uPlot.paths.bars({size:[.57,44,1],radius:.09}), points:{show:false},
    },{
      label:"缓存读取",stroke:()=>color("sub"),width:1.8,dash:[4,3],spanGaps:false,
      points:{show:()=>days<=32 || rows.length<=32 || isolated.length>0,
        filter:()=>days<=32 || rows.length<=32 ? null : isolated,size:5,fill:()=>color("bg"),stroke:()=>color("sub"),width:1.4},
    }],
  },series,host);
  const band = document.createElement("i");
  band.className = "day-band"; band.hidden = true;
  chart.over.prepend(band);
  let tipWidth, tipHeight;
  function show(day, left, topPx) {
    if (!Number.isFinite(day)) return;
    day = Math.min(last,Math.max(first,Math.round(day)));
    if (day!==selected) {
      selected=day;
      const row=byDay.get(day);
      tip.innerHTML = `<strong>${dateString(day)}</strong>`+(row
        ? ["total","input","output","cache_read"].map(key=>`<div><span>${esc(metNames[key])}</span><b>${full(row[key])}${row[key]!=null && row[key+"_n"]<row.n ? ` <small>${full(row[key+"_n"])}/${full(row.n)} 条</small>` : ""}</b></div>`).join("") +
          (row.value != null ? `<div class="tip-value"><span>等效 API 价值</span><b>${formatUsd(row.value)}</b></div>` : "")
        : "<p>当日无实测记录</p>");
      tip.hidden=false;
      tipWidth=tip.offsetWidth; tipHeight=tip.offsetHeight;
      const col=Math.max(1,chart.over.clientWidth/days);
      band.hidden=false; band.style.width=col+"px";
      band.style.transform=`translateX(${chart.valToPos(day,"x")-col/2}px)`;
    }
    const x=Math.max(0,Math.min(width-tipWidth,left+tipWidth+14>width ? left-tipWidth-9 : left+12));
    const y=Math.max(0,Math.min(height-tipHeight,topPx-20));
    tip.style.translate=`${x}px ${y}px`;
  }
  const plotArea=chart.over;
  function hover(e) {
    const rect=plotArea.getBoundingClientRect(), frame=el.getBoundingClientRect();
    const left=e.clientX-rect.left;
    show(chart.posToVal(left,"x"),e.clientX-frame.left,e.clientY-frame.top);
  }
  function hide() { tip.hidden=true; band.hidden=true; selected=null; }
  function key(e) {
    if (e.key==="Escape") { hide(); chart.setCursor({left:-10,top:-10}); return; }
    if (!["ArrowLeft","ArrowRight","Home","End"].includes(e.key)) return;
    e.preventDefault();
    const next=e.key==="Home"?first:e.key==="End"?last:selected==null?(e.key==="ArrowRight"?first:last):selected+(e.key==="ArrowRight"?1:-1);
    const day=Math.max(first,Math.min(last,next)), pos=chart.valToPos(day,"x");
    chart.setCursor({left:pos,top:plotArea.clientHeight/2});
    show(day,pos+plotArea.offsetLeft,18);
  }
  plotArea.addEventListener("pointermove",hover);
  plotArea.addEventListener("pointerdown",hover);
  plotArea.addEventListener("pointerleave",hide);
  el.addEventListener("keydown",key); el.addEventListener("blur",hide);
  const observer=new ResizeObserver(()=>{
    if (!el.isConnected || !el.clientWidth || (el.clientWidth===width && el.clientHeight===height)) return;
    width=el.clientWidth; height=el.clientHeight;
    hide(); chart.setSize({width,height});
  });
  observer.observe(el);
  return {update(records,range){
    rows=records;
    setRange(records,range);
    missing.hidden=hasData;
    hide(); chart.setCursor({left:-10,top:-10});
    chart.setData(series);
  },theme(){
    gradient=null; hide(); chart.redraw(true,true);
  },disconnect(){
    observer.disconnect(); plotArea.removeEventListener("pointermove",hover);
    plotArea.removeEventListener("pointerdown",hover); plotArea.removeEventListener("pointerleave",hide);
    el.removeEventListener("keydown",key); el.removeEventListener("blur",hide); chart.destroy(); tip.remove();
  }};
}
