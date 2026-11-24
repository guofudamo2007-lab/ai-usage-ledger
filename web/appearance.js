import { $, $$, api, post, option, toast, icon } from "./ui.js";

const systemDark = matchMedia("(prefers-color-scheme: dark)");
const effective = s => s.mode === "system" ? (systemDark.matches ? "dark" : "light") : s.mode;
let saved, draft, catalogue;
const paletteKeys = ["强调色", "背景色", "文字色"];
const fontNames = {"系统默认":'"Segoe UI Variable Text","Microsoft YaHei UI",sans-serif', "等宽":'"IBM Plex Mono",Consolas,"Microsoft YaHei UI",monospace'};
let frame;
function luminance(hex) {
  const [r,g,b]=hex.slice(1).match(/../g).map(x=>parseInt(x,16)/255)
    .map(x=>x<=.04045 ? x/12.92 : ((x+.055)/1.055)**2.4);
  return r*.2126+g*.7152+b*.0722;
}
function legible(ink, background) {
  const a=luminance(ink), b=luminance(background);
  if ((Math.max(a,b)+.05)/(Math.min(a,b)+.05)>=4.5) return ink;
  return b>.179 ? "#000000" : "#FFFFFF";
}
function apply(s) {
  const mode = effective(s), root = document.documentElement;
  const [accent,bg,fg] = s[mode];
  const ink=legible(fg,bg), base=luminance(bg), mark=luminance(accent);
  root.toggleAttribute("data-quiet-accent",(Math.max(base,mark)+.05)/(Math.min(base,mark)+.05)<2);
  root.toggleAttribute("data-strong-type",base>.08 && base<.38);
  root.style.colorScheme = mode;
  root.classList.toggle("dark",mode === "dark");
  root.dataset.mode = mode;
  for (const [key,value] of Object.entries({accent,bg,fg:ink})) root.style.setProperty("--"+key,value);
  root.style.setProperty("--on-accent",legible("#FFFFFF",accent));
  for (const variant of ["light","dark"]) {
    const [mark,base,text]=s[variant];
    root.style.setProperty("--preview-"+variant,base);
    root.style.setProperty("--preview-"+variant+"-ink",legible(text,base));
    root.style.setProperty("--preview-"+variant+"-accent",mark);
  }
  root.style.setProperty("--ui",fontNames[s.font] || `"${s.font}", "Microsoft YaHei UI", sans-serif`);
  cancelAnimationFrame(frame);
  frame = requestAnimationFrame(()=>window.dispatchEvent(new Event("meter-theme")));
}

export async function initAppearance(zone) {
  $("#open-widget").onclick = async () => {
    try {await post("/api/widget/open?tz="+encodeURIComponent(zone()),{});}
    catch(err){toast(err.message);}
  };
  const dialog = $("#appearance-dialog");
  const answer = await api("/api/appearance");
  catalogue = answer; saved = answer.settings; apply(saved);
  const modes = [["system","跟随系统"],["light","浅色"],["dark","深色"]];
  function syncFields() {
    const colors=draft[effective(draft)];
    for (const el of $$("[data-mode]",dialog)) el.setAttribute("aria-pressed",String(el.dataset.mode===draft.mode));
    for (let i=0;i<3;i++) {
      $("#paint"+i,dialog).value=colors[i];
      $("#hex"+i,dialog).value=colors[i]; $("#hex"+i,dialog).setCustomValidity("");
    }
    $("#preset",dialog).value=draft.preset;
    $("#ui-font",dialog).value=draft.font;
    apply(draft);
  }
  function resetPalette(key) {
    const preset=catalogue.presets[key];
    Object.assign(draft,{preset:key,light:[...preset.light],dark:[...preset.dark]});
  }
  $("#appearance").onclick = () => {
    draft=structuredClone(saved);
    dialog.innerHTML=`<form><div class="appearance-head"><h2 id="appearance-title">外观</h2><button type="button" class="close-appearance" aria-label="关闭外观">${icon("x")}</button></div>
      <div class="appearance-content"><fieldset><legend>显示模式</legend><div class="mode-cards">${modes.map(([id,label])=>`<button type="button" data-mode="${id}"><i class="mode-preview ${id}" aria-hidden="true"><b></b></i><span>${label}</span></button>`).join("")}</div></fieldset>
      <label class="appearance-row">主题预设<select id="preset">${Object.entries(catalogue.presets).map(([k,v])=>option(k,v.name,draft.preset)).join("")}</select></label>
      ${paletteKeys.map((name,i)=>`<div class="appearance-row"><label for="hex${i}">${name}</label><div class="hex-control"><input type="color" id="paint${i}" aria-label="选择${name}"><input type="text" id="hex${i}" aria-label="${name} HEX" maxlength="7" required pattern="#[a-fA-F0-9]{6}" spellcheck="false"></div></div>`).join("")}
      <label class="appearance-row">界面字体<select id="ui-font">${catalogue.fonts.map(f=>option(f,f,draft.font)).join("")}</select></label></div>
      <div class="appearance-foot"><button type="button" class="text-btn" id="reset-appearance">恢复默认</button><button class="primary" type="submit">保存外观</button></div></form>`;
    for(const b of $$("[data-mode]",dialog)) b.onclick=()=>{draft.mode=b.dataset.mode;syncFields();};
    $("#preset",dialog).onchange=e=>{resetPalette(e.target.value);syncFields();};
    $("#ui-font",dialog).onchange=e=>{draft.font=e.target.value;apply(draft);};
    for(let i=0;i<3;i++) {
      const paint=$("#paint"+i,dialog), hex=$("#hex"+i,dialog);
      paint.oninput=()=>{hex.value=paint.value.toUpperCase();hex.dispatchEvent(new Event("input"));};
      hex.oninput=()=>{
        if(!/^#[0-9a-f]{6}$/i.test(hex.value))return;
        draft[effective(draft)][i]=hex.value.toUpperCase();paint.value=hex.value;apply(draft);
      };
    }
    $(".close-appearance",dialog).onclick=()=>dialog.close();
    $("#reset-appearance",dialog).onclick=()=>{resetPalette("ink");draft.mode="dark";draft.font="系统默认";syncFields();};
    $("form",dialog).onsubmit=async e=>{
      e.preventDefault();
      const submit=e.submitter;if(submit)submit.disabled=true;
      try {saved=(await post("/api/appearance",draft)).settings;dialog.close();toast("外观已保存");}
      catch(err){toast(err.message);} finally{if(submit)submit.disabled=false;}
    };
    syncFields();dialog.showModal();
  };
  dialog.addEventListener("close",()=>{draft=null;apply(saved);});
  systemDark.addEventListener("change",()=>{if(draft)syncFields();else apply(saved);});
}
