"""Portable, per-ledger appearance preferences shared with the desktop widget."""
import re
from .codex_search import save, setting

PRESETS = {
    "ink": {"name": "墨与铜", "dark": ["#E79B69","#171719","#F2EEE8"], "light": ["#A65027","#F5F1EB","#302B28"]},
    "graphite": {"name": "石墨蓝", "dark": ["#89ACFA","#18191B","#EDEEF0"], "light": ["#0169CC","#FAFAFA","#15171A"]},
    "plum": {"name": "暮紫", "dark": ["#BAA5F2","#1C1921","#F0ECF4"], "light": ["#78519D","#F6F3F8","#302A36"]},
}
FONTS = ("系统默认", "Microsoft YaHei UI", "Segoe UI", "等宽")


def checked(value):
    if not isinstance(value, dict):
        raise ValueError("外观设置格式无效")
    preset, mode, font = value.get("preset","ink"), value.get("mode","dark"), value.get("font",FONTS[0])
    if preset not in PRESETS or mode not in ("light","dark","system") or font not in FONTS:
        raise ValueError("请选择有效的模式、主题和字体")
    result = dict(preset=preset, mode=mode, font=font)
    for key in ("light","dark"):
        colors = value.get(key, PRESETS[preset][key])
        if not isinstance(colors,list) or len(colors)!=3 or not all(isinstance(c,str) and re.fullmatch("#[0-9A-Fa-f]{6}",c) for c in colors):
            raise ValueError("颜色须为六位 HEX 色值")
        result[key] = [c.upper() for c in colors]
    return result


def read(db):
    return checked(setting(db,"appearance",{}))


def write(db, value):
    result = checked(value)
    with db:
        save(db,"appearance",result)
    return result
