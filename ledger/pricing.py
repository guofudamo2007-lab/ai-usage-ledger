"""Offline public-rate equivalent API value estimator & per-ledger rate overrides."""

import math
import re

from .codex_search import save, setting

CATALOG_VERSION = "2026-11 公开标价基准"
SETTINGS_KEY = "pricing_overrides"

_PROVIDER_DOT_RE = re.compile(
    r"^(?:(?:us|eu|ap|apac|global)\.)?(?:anthropic|openai|google|xai|x-ai|moonshot|moonshotai|deepseek|minimax|zhipu|z-ai|qwen|alibaba)\."
)
_MODEL_ENUM_RE = re.compile(
    r"^model-(?=(?:claude|gpt|codex|o[134]|gemini|grok|kimi|moonshot|deepseek|minimax|glm|qwen)\b)"
)

# (rule_id, label, vendor, regex, input, cache_read, cache_write, output) in USD per 1M tokens.
# Ordered most-specific first; conservative matching avoids fabricating rates for unknown/fictional models.
CATALOG = (
    # Anthropic
    ("claude-fable-5.2", "Claude Fable 5.2", "Anthropic", r"^claude-(?:fable-5[.-]2|5[.-]2-fable)\b", 8.0, 0.20, 10.0, 40.0),
    ("claude-fable-5", "Claude Fable 5 / 5.1", "Anthropic", r"^claude-(?:fable-5(?:[.-][01])?|5(?:[.-][01])?-fable)\b", 10.0, 0.25, 12.5, 50.0),
    ("claude-opus-5.5", "Claude Opus 5.5", "Anthropic", r"^claude-(?:opus-5[.-]5|5[.-]5-opus)\b", 4.0, 0.20, 5.0, 20.0),
    ("claude-opus-4.5-5", "Claude Opus 4.5–4.8 / 5", "Anthropic", r"^claude-(?:opus-(?:5(?![.-]\d)|4[.-][5-9]\d*)|(?:5(?![.-]\d)|4[.-][5-9]\d*)-opus)\b", 5.0, 0.50, 6.25, 25.0),
    ("claude-opus-4-3", "Claude Opus 3 / 4 / 4.1", "Anthropic", r"^claude-(?:opus-(?:4(?:[.-][01])?|3)|(?:4(?:[.-][01])?|3)-opus)\b", 15.0, 1.50, 18.75, 75.0),
    ("claude-sonnet-5", "Claude Sonnet 5 / 5.5", "Anthropic", r"^claude-(?:sonnet-5(?:[.-]\d+)?|5(?:[.-]\d+)?-sonnet)\b", 2.0, 0.10, 2.50, 10.0),
    ("claude-sonnet-4", "Claude Sonnet 3.5–4.6", "Anthropic", r"^claude-(?:sonnet-(?:4(?:[.-]\d+)?|3(?:[.-][057])?)|(?:4(?:[.-]\d+)?|3(?:[.-][057])?)-sonnet)\b", 3.0, 0.30, 3.75, 15.0),
    ("claude-haiku-5", "Claude Haiku 5 / 5.5", "Anthropic", r"^claude-(?:haiku-5(?:[.-]\d+)?|5(?:[.-]\d+)?-haiku)\b", 0.10, 0.01, 0.125, 0.50),
    ("claude-haiku-4", "Claude Haiku 4 / 4.5", "Anthropic", r"^claude-(?:haiku-4(?:[.-]\d+)?|4(?:[.-]\d+)?-haiku)\b", 1.0, 0.10, 1.25, 5.0),
    ("claude-haiku-3.5", "Claude Haiku 3.5", "Anthropic", r"^claude-(?:3[.-]5-haiku|haiku-3[.-]5)\b", 0.80, 0.08, 1.00, 4.0),
    ("claude-haiku-3", "Claude Haiku 3", "Anthropic", r"^claude-(?:3-haiku|haiku-3)\b", 0.25, 0.03, 0.30, 1.25),
    # OpenAI
    # Standard short-context rates, using the 30-minute cache-write tier.
    ("gpt-6.2-astra", "GPT-6.2 Astra", "OpenAI", r"^(?:gpt-6[.-]2-astra|(?:gpt-)?astra-6[.-]2)(?![.-]\d)\b", 8, 0.4, 10, 40),
    ("gpt-6-astra", "GPT-6 Astra", "OpenAI", r"^(?:gpt-6-astra|(?:gpt-)?astra-6)(?![.-]\d)\b", 10, 1, 12.5, 50),
    ("gpt-6.1-2-sol", "GPT-6.1 / 6.2 Sol", "OpenAI", r"^(?:gpt-6[.-][12]-sol|(?:gpt-)?sol-6[.-][12])(?![.-]\d)\b", 2, 0.1, 2.5, 10),
    ("gpt-6-sol", "GPT-6 Sol", "OpenAI", r"^(?:gpt-6-sol|(?:gpt-)?sol-6)(?![.-]\d)\b", 2, 0.2, 2.5, 10),
    ("gpt-6.1-terra", "GPT-6.1 Terra", "OpenAI", r"^(?:gpt-6[.-]1-terra|(?:gpt-)?terra-6[.-]1)(?![.-]\d)\b", 1, 0.05, 1.25, 5),
    ("gpt-6-luna", "GPT-6 Luna", "OpenAI", r"^(?:gpt-6-luna|(?:gpt-)?luna-6)(?![.-]\d)\b", 0.1, 0.01, 0.125, 0.5),
    ("gpt-5.6-luna", "GPT-5.6 Luna", "OpenAI", r"^gpt-5[.-]6-luna\b", 0.20, 0.02, 0.20, 1.20),
    ("gpt-5.6", "GPT-5.6 / Sol", "OpenAI", r"^gpt-5[.-]6(?:-sol)?\b", 4.0, 0.40, 4.0, 20.0),
    ("gpt-5.4-5.5-pro", "GPT-5.4 / 5.5 Pro", "OpenAI", r"^gpt-5[.-][45]-pro\b", 30.0, 3.0, 30.0, 180.0),
    ("gpt-5.5", "GPT-5.5", "OpenAI", r"^gpt-5[.-]5\b", 5.0, 0.50, 5.0, 30.0),
    ("gpt-5.4-mini", "GPT-5.4 mini", "OpenAI", r"^gpt-5[.-]4(?:-[a-z0-9]+)*-mini\b", 0.75, 0.075, 0.75, 4.50),
    ("gpt-5.4-nano", "GPT-5.4 nano", "OpenAI", r"^gpt-5[.-]4(?:-[a-z0-9]+)*-nano\b", 0.20, 0.02, 0.20, 1.25),
    ("gpt-5.4", "GPT-5.4", "OpenAI", r"^gpt-5[.-]4\b", 2.50, 0.25, 2.50, 15.0),
    ("gpt-5.2-3-pro", "GPT-5.2 / 5.3 Pro", "OpenAI", r"^gpt-5[.-][23]-pro\b", 21.0, 2.10, 21.0, 168.0),
    ("gpt-5-pro", "GPT-5 Pro", "OpenAI", r"^gpt-5(?:[.-]1)?-pro\b", 15.0, 1.50, 15.0, 120.0),
    ("gpt-5-mini", "GPT-5 mini", "OpenAI", r"^gpt-5(?:[.-][0-3])?(?:-[a-z0-9]+)*-mini\b", 0.25, 0.025, 0.25, 2.0),
    ("gpt-5-nano", "GPT-5 nano", "OpenAI", r"^gpt-5(?:[.-][0-3])?(?:-[a-z0-9]+)*-nano\b", 0.05, 0.005, 0.05, 0.40),
    ("gpt-5.2-3", "GPT-5.2 / 5.3 Codex", "OpenAI", r"^gpt-5[.-][23](?:-codex)?\b", 1.75, 0.175, 1.75, 14.0),
    ("gpt-5", "GPT-5 / Codex", "OpenAI", r"^(?:gpt-5(?:[.-][01])?(?:-codex)?|codex-1)\b", 1.25, 0.125, 1.25, 10.0),
    ("codex-mini", "Codex mini", "OpenAI", r"^codex-mini\b", 1.50, 0.375, 1.50, 6.0),
    ("gpt-oss-120b", "GPT-OSS 120B", "OpenAI", r"^gpt-oss-120b\b", 0.15, 0.075, 0.15, 0.60),
    ("gpt-oss-20b", "GPT-OSS 20B", "OpenAI", r"^gpt-oss-20b\b", 0.05, 0.025, 0.05, 0.20),
    ("gpt-4.5", "GPT-4.5", "OpenAI", r"^gpt-4[.-]5\b", 75.0, 37.50, 75.0, 150.0),
    ("gpt-4.1-mini", "GPT-4.1 mini", "OpenAI", r"^gpt-4[.-]1-mini\b", 0.40, 0.10, 0.40, 1.60),
    ("gpt-4.1-nano", "GPT-4.1 nano", "OpenAI", r"^gpt-4[.-]1-nano\b", 0.10, 0.025, 0.10, 0.40),
    ("gpt-4.1", "GPT-4.1", "OpenAI", r"^gpt-4[.-]1\b", 2.0, 0.50, 2.0, 8.0),
    ("gpt-4o-mini", "GPT-4o mini", "OpenAI", r"^gpt-4o-mini\b", 0.15, 0.075, 0.15, 0.60),
    ("gpt-4o", "GPT-4o", "OpenAI", r"^(?:chatgpt-)?gpt-4o\b|^chatgpt-4o\b", 2.50, 1.25, 2.50, 10.0),
    ("gpt-4-turbo", "GPT-4 Turbo", "OpenAI", r"^gpt-4-turbo\b", 10.0, 10.0, 10.0, 30.0),
    ("gpt-4", "GPT-4", "OpenAI", r"^gpt-4(?:-32k|-0\d{3}|-vision)?\b", 30.0, 30.0, 30.0, 60.0),
    ("gpt-3.5-turbo", "GPT-3.5 Turbo", "OpenAI", r"^gpt-3[.-]5-turbo\b", 0.50, 0.50, 0.50, 1.50),
    ("o3-pro", "o3-pro", "OpenAI", r"^o3-pro\b", 20.0, 5.0, 20.0, 80.0),
    ("o3-deep-research", "o3-deep-research", "OpenAI", r"^o3-deep-research\b", 10.0, 2.50, 10.0, 40.0),
    ("o4-mini-deep-research", "o4-mini-deep-research", "OpenAI", r"^o4-mini-deep-research\b", 2.0, 0.50, 2.0, 8.0),
    ("o3-o4-mini", "o3-mini / o4-mini", "OpenAI", r"^(?:o3|o4)-mini\b", 1.10, 0.275, 1.10, 4.40),
    ("o3", "o3", "OpenAI", r"^o3\b", 2.0, 0.50, 2.0, 8.0),
    ("o1-pro", "o1-pro", "OpenAI", r"^o1-pro\b", 150.0, 150.0, 150.0, 600.0),
    ("o1-mini", "o1-mini", "OpenAI", r"^o1-mini\b", 1.10, 0.55, 1.10, 4.40),
    ("o1", "o1", "OpenAI", r"^o1\b", 15.0, 7.50, 15.0, 60.0),
    # Google
    ("gemini-4-neon", "Gemini 4 Neon", "Google", r"^gemini-4(?:[.-]\d+)?-neon\b", 3.0, 0.15, 3.0, 15.0),
    ("gemini-4-carbon-argon", "Gemini 4 Carbon / Argon", "Google", r"^gemini-4(?:[.-]\d+)?-(?:carbon|argon)\b", 2.0, 0.10, 2.0, 10.0),
    ("gemini-4-hydrogen", "Gemini 4 Hydrogen", "Google", r"^gemini-4(?:[.-]\d+)?-hydrogen\b", 1.50, 0.075, 1.50, 7.50),
    ("gemini-3.6-3.8-flash", "Gemini 3.6 / 3.8 Flash", "Google", r"^gemini-3[.-][678]-flash\b", 0.75, 0.075, 0.75, 3.75),
    ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite", "Google", r"^gemini-3[.-]5-flash-lite\b", 0.30, 0.03, 0.30, 2.50),
    ("gemini-3.5-flash", "Gemini 3.5 Flash", "Google", r"^gemini-3[.-]5-flash\b", 1.50, 0.15, 1.50, 9.0),
    ("gemini-3-flash-lite", "Gemini 3.1 Flash-Lite", "Google", r"^gemini-3(?:[.-][01])?-flash-lite\b", 0.25, 0.025, 0.25, 1.50),
    ("gemini-3-pro", "Gemini 3 Pro", "Google", r"^gemini-3(?:[.-]\d+)?-pro\b", 2.0, 0.20, 2.0, 12.0),
    ("gemini-3-flash", "Gemini 3 Flash", "Google", r"^gemini-3(?:[.-][01])?-flash\b", 0.50, 0.05, 0.50, 3.0),
    ("gemini-2.5-pro", "Gemini 2.5 Pro", "Google", r"^gemini-2[.-]5-pro\b", 1.25, 0.125, 1.25, 10.0),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite", "Google", r"^gemini-2[.-]5-flash-lite\b", 0.10, 0.01, 0.10, 0.40),
    ("gemini-2.5-flash", "Gemini 2.5 Flash", "Google", r"^gemini-2[.-]5-flash\b", 0.30, 0.03, 0.30, 2.50),
    ("gemini-2.0-flash-lite", "Gemini 2.0 Flash-Lite", "Google", r"^gemini-2[.-]0-flash-lite\b", 0.075, 0.01875, 0.075, 0.30),
    ("gemini-2.0-flash", "Gemini 2.0 Flash", "Google", r"^gemini-2[.-]0-flash\b", 0.10, 0.025, 0.10, 0.40),
    ("gemini-1.5-pro", "Gemini 1.5 Pro", "Google", r"^gemini-1[.-]5-pro\b", 1.25, 0.3125, 1.25, 5.0),
    ("gemini-1.5-flash-8b", "Gemini 1.5 Flash-8B", "Google", r"^gemini-1[.-]5-flash-8b\b", 0.0375, 0.01, 0.0375, 0.15),
    ("gemini-1.5-flash", "Gemini 1.5 Flash", "Google", r"^gemini-1[.-]5-flash\b", 0.075, 0.01875, 0.075, 0.30),
    # xAI
    ("grok-4.5+", "Grok 4.5 / 4.6 / 4.7", "xAI", r"^grok-4[.-][567]\d*\b", 2.0, 0.50, 2.0, 6.0),
    ("grok-4.2-3", "Grok 4.2 / 4.3", "xAI", r"^grok-4[.-][23]\d*\b", 1.25, 0.20, 1.25, 2.50),
    ("grok-4-fast", "Grok 4 Fast", "xAI", r"^grok-4(?:[.-]1)?-fast\b", 0.20, 0.05, 0.20, 0.50),
    ("grok-code-fast-1", "Grok Code Fast 1", "xAI", r"^grok-code-fast-1\b", 0.20, 0.02, 0.20, 1.50),
    ("grok-code-fast", "Grok Code Fast", "xAI", r"^(?:grok-code-fast|grok-build-0[.-]1)\b", 1.0, 0.20, 1.0, 2.0),
    ("grok-3-mini", "Grok 3 mini", "xAI", r"^grok-3-mini\b", 0.30, 0.075, 0.30, 0.50),
    ("grok-3-4", "Grok 3 / Grok 4", "xAI", r"^grok-[34](?:\b|-beta|-latest|-0\d{3})", 3.0, 0.75, 3.0, 15.0),
    ("grok-2", "Grok 2", "xAI", r"^grok-2\b", 2.0, 0.50, 2.0, 10.0),
    # Moonshot AI
    ("kimi-k3.1-highspeed", "Kimi K3.1 HighSpeed", "Moonshot AI", r"^kimi-k3(?:[.-]1)?(?:-[a-z0-9]+)*-highspeed\b", 4.0, 0.20, 4.0, 20.0),
    ("kimi-k3.1", "Kimi K3.1 Pro", "Moonshot AI", r"^kimi-k3[.-]1\b", 2.0, 0.20, 2.0, 10.0),
    ("kimi-k3", "Kimi K3", "Moonshot AI", r"^kimi-k3\b", 3.0, 0.30, 3.0, 15.0),
    ("kimi-k2.7-highspeed", "Kimi K2.7 HighSpeed", "Moonshot AI", r"^kimi-k2[.-]7(?:-[a-z0-9]+)*-highspeed\b", 1.90, 0.38, 1.90, 8.0),
    ("kimi-k2.6-7", "Kimi K2.6 / K2.7", "Moonshot AI", r"^kimi-k2[.-][67]\b", 0.95, 0.16, 0.95, 4.0),
    ("kimi-k2", "Kimi K2 / Code", "Moonshot AI", r"^(?:kimi-k2(?:[.-]\d+)?|kimi-for-coding|kimi-code)\b", 0.60, 0.15, 0.60, 2.50),
    ("moonshot-v1-128k", "Moonshot v1 128K", "Moonshot AI", r"^moonshot-v1-128k\b", 2.0, 0.20, 2.0, 5.0),
    ("moonshot-v1-32k", "Moonshot v1 32K", "Moonshot AI", r"^moonshot-v1-32k\b", 1.0, 0.10, 1.0, 3.0),
    ("moonshot-v1-8k", "Moonshot v1 8K", "Moonshot AI", r"^moonshot-v1-(?:8k|auto)\b", 0.20, 0.02, 0.20, 2.0),
    # DeepSeek
    ("deepseek-v4-pro", "DeepSeek V4 Pro", "DeepSeek", r"^deepseek-(?:v4-)?pro\b", 0.66, 0.022, 0.66, 1.98),
    ("deepseek-flash", "DeepSeek Flash", "DeepSeek", r"^deepseek-(?:v4(?:[.-]\d+)?-)?flash\b", 0.15, 0.003, 0.15, 0.60),
    ("deepseek-r1", "DeepSeek R1", "DeepSeek", r"^deepseek-(?:reasoner|r1)\b", 0.55, 0.14, 0.55, 2.19),
    ("deepseek-v3", "DeepSeek V3 / Chat", "DeepSeek", r"^deepseek-(?:chat|v3(?:[.-]\d+)?)\b", 0.28, 0.028, 0.28, 0.42),
    # MiniMax
    ("minimax-m3.1-flash", "MiniMax M3.1 Flash", "MiniMax", r"^minimax-m3[.-]1-flash\b", 0.15, 0.015, 0.15, 0.60),
    ("minimax-m2-m3", "MiniMax M2 / M3", "MiniMax", r"^minimax-m[23](?:[.-]\d+)?\b", 0.30, 0.06, 0.30, 1.20),
    ("minimax-m1", "MiniMax M1", "MiniMax", r"^minimax-(?:m1|text-01)\b", 0.40, 0.08, 0.40, 2.20),
    # Zhipu AI
    ("glm-5.2-3-flashx", "GLM-5.3 FlashX", "Zhipu AI", r"^glm-5[.-][23]-flashx\b", 0.37, 0.075, 0.37, 1.25),
    ("glm-5.2-3-flash", "GLM-5.3 Flash", "Zhipu AI", r"^glm-5[.-][23]-flash\b", 0.15, 0.03, 0.15, 0.50),
    ("glm-5.2-3", "GLM-5.2 / 5.3", "Zhipu AI", r"^glm-5[.-][23]\b", 1.40, 0.26, 1.40, 4.40),
    ("glm-5", "GLM-5", "Zhipu AI", r"^glm-5\b", 1.0, 0.20, 1.0, 3.20),
    ("glm-4-flashx", "GLM-4 FlashX", "Zhipu AI", r"^glm-4(?:[.-]\d+)?-flashx\b", 0.07, 0.01, 0.07, 0.40),
    ("glm-4-flash", "GLM-4 Flash (Free)", "Zhipu AI", r"^glm-4(?:[.-]\d+)?-flash\b", 0.0, 0.0, 0.0, 0.0),
    ("glm-4.5-7", "GLM-4.5–4.7", "Zhipu AI", r"^glm-4[.-][567]\b", 0.60, 0.11, 0.60, 2.20),
    # Alibaba Cloud
    ("qwen-4-max", "Qwen 4 Max", "Alibaba Cloud", r"^qwen-?4-max\b", 1.50, 0.075, 1.50, 4.50),
    ("qwen-3.8-max", "Qwen 3.8 Max", "Alibaba Cloud", r"^qwen-?3[.-]8-max\b", 2.0, 0.20, 2.0, 6.0),
    ("qwen-3.7-max", "Qwen 3.7 Max", "Alibaba Cloud", r"^qwen-?3[.-]7-max\b", 2.50, 0.25, 2.50, 7.50),
    ("qwen-3.8-flash", "Qwen 3.8 Flash", "Alibaba Cloud", r"^qwen-?3[.-]8-flash\b", 0.15, 0.015, 0.15, 0.47),
    ("qwen-3-flash", "Qwen 3 Flash", "Alibaba Cloud", r"^qwen-?3(?:[.-]7)?-flash\b", 0.03, 0.003, 0.03, 0.13),
    ("qwen-plus", "Qwen Plus", "Alibaba Cloud", r"^qwen-?(?:3(?:[.-][78])?-)?plus\b", 0.40, 0.04, 0.40, 1.60),
    ("qwen-3-coder", "Qwen 3 Coder", "Alibaba Cloud", r"^qwen-?3-coder\b", 1.0, 0.10, 1.0, 5.0),
    ("qwen-max", "Qwen Max", "Alibaba Cloud", r"^qwen-?(?:3-)?max\b", 1.60, 0.16, 1.60, 6.40),
    ("qwen-turbo", "Qwen Turbo", "Alibaba Cloud", r"^qwen-?turbo\b", 0.05, 0.005, 0.05, 0.20),
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


def _normalize_model(name):
    low = name.strip().lower()
    if "/" in low:
        low = low.rsplit("/", 1)[-1]
    low = _PROVIDER_DOT_RE.sub("", low)
    low = re.sub(r"[\s_]+", "-", low).strip("-")
    return _MODEL_ENUM_RE.sub("", low)


def resolve(model, overrides=None):
    if not isinstance(model, str):
        return None
    name = model.strip()
    if not name or "型号未" in name:
        return None
    if overrides and name in overrides:
        return {"source": "override", "rule": "自定义单价", **overrides[name]}
    low = _normalize_model(name)
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
