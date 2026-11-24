import { $, $$, esc, icon, full, percent, post, toast, vendorMark, formatUsd, usdHtml } from "./ui.js";

export function openPricingModal(initialPricing, { onChanged, fetchPricing }) {
  const modal = $("#modal");
  let active = initialPricing;
  let editingModel = null;

  function draw() {
    const p = active || { version: "", total: null, priced_n: 0, total_n: 0, models: [] };
    const total = p.total;
    const saved = p.saved ?? 0;
    const noCache = p.no_cache ?? 0;
    const saveShare = noCache > 0 && saved > 0 ? Math.min(100, (100 * saved) / noCache) : 0;

    const components = [
      ["常规输入", p.input],
      ["缓存读取", p.cache_read],
      ["缓存写入", p.cache_write],
      ["输出（含推理）", p.output],
    ]
      .map(
        ([label, val]) => `<div class="pricing-stat"><span>${label}</span><strong class="num">${formatUsd(val)}</strong><small>${percent(val, total)}</small></div>`,
      )
      .join("");

    const savingsBar =
      saved >= 0.005
        ? `<div class="pricing-savings">
            <div class="pricing-savings-head">
              <span>若无 Prompt Caching 原价 <b class="num">${formatUsd(noCache)}</b></span>
              <span class="value-saved">缓存约省 <b class="num">${formatUsd(saved)}</b>（${saveShare.toFixed(1)}%）</span>
            </div>
            <div class="pricing-savings-track"><i style="width:${saveShare.toFixed(1)}%"></i></div>
          </div>`
        : "";

    const rowsHtml = (p.models || [])
      .map((m) => {
        const isEditing = editingModel === m.model;
        const rates = m.rates || { input: 1.0, cache_read: 0.1, cache_write: 1.0, output: 5.0 };
        const stateTag =
          m.source === "override"
            ? `<span class="rate-badge custom">自定义单价</span>`
            : m.source === "builtin"
              ? `<span class="rate-badge">${esc(m.rule)}</span>`
              : `<span class="rate-badge unpriced">未定价</span>`;
        const rateText = m.rates
          ? `输 $${m.rates.input} · 缓读 $${m.rates.cache_read} · 缓写 $${m.rates.cache_write} · 出 $${m.rates.output} <small>/1M</small>`
          : "暂无公开标价映射 · 点击右侧设单价即可纳入估算";

        const editor = isEditing
          ? `<form class="pricing-editor" data-model-form="${esc(m.model)}">
              <label><span>输入 $/1M</span><input name="input" type="number" step="any" min="0" max="10000" required value="${esc(rates.input)}"></label>
              <label><span>缓存读取 $/1M</span><input name="cache_read" type="number" step="any" min="0" max="10000" required value="${esc(rates.cache_read)}"></label>
              <label><span>缓存写入 $/1M</span><input name="cache_write" type="number" step="any" min="0" max="10000" required value="${esc(rates.cache_write)}"></label>
              <label><span>输出 $/1M</span><input name="output" type="number" step="any" min="0" max="10000" required value="${esc(rates.output)}"></label>
              <div class="pricing-editor-actions">
                <button type="button" class="small-btn" data-cancel-edit>取消</button>
                <button type="submit" class="primary">保存单价</button>
              </div>
            </form>`
          : "";

        return `<div class="pricing-item">
          <div class="pricing-row">
            <div class="pricing-id">${vendorMark(m.vendor)}<div><b>${esc(m.model)}</b><div class="pricing-meta">${stateTag}<span>${rateText}</span></div></div></div>
            <div class="pricing-right">
              <div class="pricing-val">${m.value == null ? '<span class="num">—</span>' : usdHtml(m.value)}<small>${full(m.priced_n)}/${full(m.n)} 条</small></div>
              <div class="pricing-btns">
                <button type="button" class="small-btn" data-edit-rate="${esc(m.model)}">${m.rates ? "调价" : "设单价"}</button>
                ${m.source === "override" ? `<button type="button" class="text-btn" data-reset-rate="${esc(m.model)}">恢复</button>` : ""}
              </div>
            </div>
          </div>
          ${editor}
        </div>`;
      })
      .join("");

    $("#modal-content").innerHTML = `<div class="pricing-modal">
      <div class="dialog-head">
        <div><h2>等效 API 价值与单价</h2></div>
        <button id="close-pricing" aria-label="关闭">${icon("x")}</button>
      </div>
      <div class="pricing-body">
        <section class="pricing-hero">
          <div class="pricing-total">${total == null ? '<span class="num">—</span><small class="usd-code">暂无可估算记录</small>' : `${usdHtml(total, "pricing-hero-usd")}<small class="usd-code">USD</small>`}</div>
          <p class="pricing-sub">${esc(p.version)} · 已定价 <span class="num">${full(p.priced_n)} / ${full(p.total_n)}</span> 条${p.cr_unknown_n ? ` · ${full(p.cr_unknown_n)} 条未报缓存读取按常规输入计` : ""}</p>
          <p class="pricing-disclaimer">按公开 API 标价估算，仅供参考</p>
        </section>
        ${total != null ? `<div class="pricing-split">${components}</div>${savingsBar}` : ""}
        <section class="pricing-section">
          <div class="pricing-section-head"><h3>当前范围模型单价</h3><span class="muted">单位：美元 / 1M Token</span></div>
          <div class="pricing-list">${rowsHtml || '<p class="muted" style="padding:14px 16px;">当前筛选范围暂无模型记录。</p>'}</div>
        </section>
      </div>
    </div>`;

    $("#close-pricing").onclick = () => modal.close();

    for (const btn of $$("[data-edit-rate]", modal)) {
      btn.onclick = () => {
        editingModel = editingModel === btn.dataset.editRate ? null : btn.dataset.editRate;
        draw();
      };
    }

    if ($("[data-cancel-edit]", modal)) {
      $("[data-cancel-edit]", modal).onclick = () => {
        editingModel = null;
        draw();
      };
    }

    for (const btn of $$("[data-reset-rate]", modal)) {
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          await post("/api/pricing", { model: btn.dataset.resetRate, reset: true });
          editingModel = null;
          await onChanged();
          active = fetchPricing();
          draw();
          toast("已恢复内置参考单价");
        } catch (err) {
          toast(err.message);
          btn.disabled = false;
        }
      };
    }

    const form = $("[data-model-form]", modal);
    if (form) {
      form.onsubmit = async (e) => {
        e.preventDefault();
        const fd = Object.fromEntries(new FormData(form));
        const submit = $('button[type="submit"]', form);
        if (submit) submit.disabled = true;
        try {
          await post("/api/pricing", {
            model: form.dataset.modelForm,
            input: Number(fd.input),
            cache_read: Number(fd.cache_read),
            cache_write: Number(fd.cache_write),
            output: Number(fd.output),
          });
          editingModel = null;
          await onChanged();
          active = fetchPricing();
          draw();
          toast("单价已保存并重算");
        } catch (err) {
          toast(err.message);
          if (submit) submit.disabled = false;
        }
      };
    }
  }

  draw();
  if (!modal.open) modal.showModal();
}
