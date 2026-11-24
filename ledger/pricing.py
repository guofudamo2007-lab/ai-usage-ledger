"""Offline public-rate equivalent API value estimator & per-ledger rate overrides."""

import math
import re

from .codex_search import save, setting

CATALOG_VERSION = "2026-11 公开标价基准"
SETTINGS_KEY = "pricing_overrides"

# (rule_id, label, vendor, regex, input, cache_read, cache_write, output) in USD per 1M tokens.
# Ordered most-specific first; conservative matching avoids fabricating rates for unknown/fictional models.
CATALOG = (
    # Anthropic
    ("claude-opus-4.5+", "Claude Opus 4.5 / 4.6", "Anthropic", r"^claude-opus-4(?:[.-][56]\d*)", 5.0, 0.50, 6.25, 25.0),
    ("claude-opus-4", "Claude Opus 4 / 4.1", "Anthropic", r"^claude-opus-4(?:[.-][01])?(?!\d)", 15.0, 1.50, 18.75, 75.0),
    ("claude-sonnet-4", "Claude Sonnet 4.x / 3.5+", "Anthropic", r"^(?:claude-sonnet-4(?:[.-]\d+)?|claude-3[.-][57]-sonnet)", 3.0, 0.30, 3.75, 15.0),
    ("claude-haiku-4", "Claude Haiku 4 / 4.5", "Anthropic", r"^claude-haiku-4(?:[.-]5)?", 1.0, 0.10, 1.25, 5.0),
    ("claude-haiku-3.5", "Claude Haiku 3.5", "Anthropic", r"^claude-3[.-]5-haiku", 0.80, 0.08, 1.00, 4.0),
    # OpenAI
    ("gpt-5.4-mini", "GPT-5.4 mini", "OpenAI", r"^gpt-5[.-]4-mini\b", 0.75, 0.075, 0.75, 4.50),
    ("gpt-5.4-nano", "GPT-5.4 nano", "OpenAI", r"^gpt-5[.-]4-nano\b", 0.20, 0.02, 0.20, 1.25),
    ("gpt-5.4", "GPT-5.4", "OpenAI", r"^gpt-5[.-]4\b", 2.50, 0.25, 2.50, 15.0),
    ("gpt-5.2-3", "GPT-5.2 / 5.3 Codex", "OpenAI", r"^gpt-5[.-][23](?:-codex)?\b", 1.75, 0.175, 1.75, 14.0),
    ("gpt-5-mini", "GPT-5 mini", "OpenAI", r"^gpt-5(?:[.-]1)?(?:-[a-z0-9]+)*-mini\b", 0.25, 0.025, 0.25, 2.0),
    ("gpt-5-nano", "GPT-5 nano", "OpenAI", r"^gpt-5(?:-codex)?-nano\b", 0.05, 0.005, 0.05, 0.40),
    ("gpt-5", "GPT-5 / Codex", "OpenAI", r"^(?:gpt-5(?:[.-]1)?(?:-codex)?|codex-1)\b", 1.25, 0.125, 1.25, 10.0),
    ("gpt-4.1-mini", "GPT-4.1 mini", "OpenAI", r"^gpt-4[.-]1-mini\b", 0.40, 0.10, 0.40, 1.60),
    ("gpt-4.1-nano", "GPT-4.1 nano", "OpenAI", r"^gpt-4[.-]1-nano\b", 0.10, 0.025, 0.10, 0.40),
    ("gpt-4.1", "GPT-4.1", "OpenAI", r"^gpt-4[.-]1\b", 2.0, 0.50, 2.0, 8.0),
    ("gpt-4o-mini", "GPT-4o mini", "OpenAI", r"^gpt-4o-mini\b", 0.15, 0.075, 0.15, 0.60),
    ("gpt-4o", "GPT-4o", "OpenAI", r"^gpt-4o\b", 2.50, 1.25, 2.50, 10.0),
    ("o3-pro", "o3-pro", "OpenAI", r"^o3-pro\b", 20.0, 5.0, 20.0, 80.0),
    ("o3-o4-mini", "o3-mini / o4-mini", "OpenAI", r"^(?:o3|o4)-mini\b", 1.10, 0.275, 1.10, 4.40),
    ("o3", "o3", "OpenAI", r"^o3\b", 2.0, 0.50, 2.0, 8.0),
    ("o1-mini", "o1-mini", "OpenAI", r"^o1-mini\b", 1.10, 0.55, 1.10, 4.40),
    ("o1", "o1", "OpenAI", r"^o1\b", 15.0, 7.50, 15.0, 60.0),
    # Google
    ("gemini-3-pro", "Gemini 3 Pro", "Google", r"^gemini-3(?:[.-]\d+)?-pro\b", 2.0, 0.20, 2.0, 12.0),
    ("gemini-3-flash", "Gemini 3 Flash", "Google", r"^gemini-3(?:[.-]\d+)?-flash\b", 0.50, 0.05, 0.50, 3.0),
    ("gemini-2.5-pro", "Gemini 2.5 Pro", "Google", r"^gemini-2[.-]5-pro\b", 1.25, 0.125, 1.25, 10.0),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", "Google", r"^gemini-2[.-]5-flash-lite\b", 0.10, 0.01, 0.10, 0.40),
    ("gemini-2.5-flash", "Gemini 2.5 Flash", "Google", r"^gemini-2[.-]5-flash\b", 0.30, 0.03, 0.30, 2.50),
    ("gemini-2.0-flash", "Gemini 2.0 Flash", "Google", r"^gemini-2[.-]0-flash\b", 0.10, 0.025, 0.10, 0.40),
    # xAI
    ("grok-4.5+", "Grok 4.5 / 4.6 / 4.7", "xAI", r"^grok-4[.-][567]\d*\b", 2.0, 0.50, 2.0, 6.0),
    ("grok-4.2-3", "Grok 4.2 / 4.3", "xAI", r"^grok-4[.-][23]\d*\b", 1.25, 0.20, 1.25, 2.50),
    ("grok-fast", "Grok Fast / Code Fast", "xAI", r"^grok-(?:4|code)-fast\b", 0.20, 0.05, 0.20, 0.50),
    ("grok-3-mini", "Grok 3 mini", "xAI", r"^grok-3-mini\b", 0.30, 0.075, 0.30, 0.50),
    ("grok-3-4", "Grok 3 / Grok 4", "xAI", r"^grok-[34](?:\b|-beta|-latest)", 3.0, 0.75, 3.0, 15.0),
    # Moonshot AI
    ("kimi-k3", "Kimi K3", "Moonshot AI", r"^kimi-k3(?:[.-]\d+)?\b", 3.0, 0.30, 3.0, 15.0),
    ("kimi-k2", "Kimi K2", "Moonshot AI", r"^kimi-k2(?:[.-]\d+)?\b", 0.60, 0.15, 0.60, 2.50),
)

_COMPILED = tuple(
    (rule_id, label, vendor, re.compile(pattern), inp, cr, cw, out)
    for rule_id, label, vendor, pattern, inp, cr, cw, out in CATALOG
)
RATE_FIELDS = ("input", "cache_read", "cache_write", "output")


def _rate_num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("单价须为数字")
    num = float(value)
    if not math.isfinite(num) or not 0.0 <= num <= 10_000.0:
        raise ValueError("单价须在 0 至 10,000 美元 / 1M Token 之间")
    return round(num, 6)


def _checked_rates(raw):
    if not isinstance(raw, dict):
        raise ValueError("单价配置格式无效")
    return {field: _rate_num(raw.get(field)) for field in RATE_FIELDS}


def read_overrides(db):
    raw = setting(db, SETTINGS_KEY, {})
    if not isinstance(raw, dict):
        return {}
    cleaned = {}
    for model, rates in raw.items():
        if isinstance(model, str) and model and len(model) <= 160:
            try:
                cleaned[model] = _checked_rates(rates)
            except ValueError:
                continue
    return cleaned


def write_override(db, payload):
    if not isinstance(payload, dict):
        raise ValueError("单价参数格式无效")
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip() or len(model.strip()) > 160:
        raise ValueError("请指定有效的模型名称")
    if any(ord(c) < 32 for c in model):
        raise ValueError("模型名称包含无效字符")
    model = model.strip()
    overrides = read_overrides(db)
    if payload.get("reset"):
        overrides.pop(model, None)
    else:
        overrides[model] = _checked_rates(payload)
    with db:
        save(db, SETTINGS_KEY, overrides)
        db.execute("UPDATE usage_revision SET value=value+1 WHERE id=1")
    return {"version": CATALOG_VERSION, "overrides": overrides}


def resolve(model, overrides=None):
    if not isinstance(model, str):
        return None
    name = model.strip()
    if not name or "型号未" in name:
        return None
    if overrides and name in overrides:
        return {"source": "override", "rule": "自定义单价", **overrides[name]}
    low = name.lower().removeprefix("models/")
    for _, label, _, pattern, inp, cr, cw, out in _COMPILED:
        if pattern.search(low):
            return {
                "source": "builtin",
                "rule": label,
                "input": inp,
                "cache_read": cr,
                "cache_write": cw,
                "output": out,
            }
    return None


def cost_parts(fresh_in, cache_read, cache_write, output, rate):
    """Return (total, input, cache_read, cache_write, output, no_cache, saved) in USD."""
    in_usd = fresh_in * rate["input"] / 1_000_000.0
    cr_usd = cache_read * rate["cache_read"] / 1_000_000.0
    cw_usd = cache_write * rate["cache_write"] / 1_000_000.0
    out_usd = output * rate["output"] / 1_000_000.0
    total = in_usd + cr_usd + cw_usd + out_usd
    no_cache = ((fresh_in + cache_read + cache_write) * rate["input"] + output * rate["output"]) / 1_000_000.0
    saved = max(0.0, no_cache - total)
    return total, in_usd, cr_usd, cw_usd, out_usd, no_cache, saved


def catalogue_payload(db):
    return {
        "version": CATALOG_VERSION,
        "overrides": read_overrides(db),
        "catalog": [
            {
                "id": rule_id,
                "label": label,
                "vendor": vendor,
                "input": inp,
                "cache_read": cr,
                "cache_write": cw,
                "output": out,
            }
            for rule_id, label, vendor, _, inp, cr, cw, out in CATALOG
        ],
    }
