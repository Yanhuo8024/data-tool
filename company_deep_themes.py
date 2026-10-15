#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
company_deep_themes.py — 内置 PPT 汇报模板主题（从 company_deep_report 拆出）
"""
# ── 内置 PPT 汇报模板 ─────────────────────────────────
# cover_mode: solid=整页色块封面 | band=浅底+顶部色带 | minimal=白底大标题
PPT_TEMPLATES = {
    "navy_business": {
        "id": "navy_business",
        "name": "商务深蓝",
        "desc": "对外汇报 / 投委会 / 正式路演",
        "accent": (0x1F, 0x3A, 0x5F),
        "accent2": (0x22, 0xD3, 0xEE),
        "panel": (0xF7, 0xF5, 0xF0),
        "ink": (0x1F, 0x1F, 0x1F),
        "muted": (0x59, 0x59, 0x59),
        "on_dark": (0xFF, 0xFF, 0xFF),
        "on_dark_sub": (0xD2, 0xDC, 0xF0),
        "ok": (0x2D, 0xD4, 0xA8),
        "warn": (0xF5, 0xC4, 0x51),
        "danger": (0xF8, 0x71, 0x71),
        "cover_mode": "solid",
        "title_size": 26,
        "stat_size": 28,
    },
    "dark_tech": {
        "id": "dark_tech",
        "name": "科技深色",
        "desc": "科技 / 互联网 / 新经济标的",
        "accent": (0x0B, 0x12, 0x20),
        "accent2": (0x22, 0xD3, 0xEE),
        "panel": (0x12, 0x1A, 0x2B),
        "ink": (0xEE, 0xF2, 0xFF),
        "muted": (0x8B, 0x9B, 0xB8),
        "on_dark": (0xFF, 0xFF, 0xFF),
        "on_dark_sub": (0xA8, 0xC0, 0xE8),
        "ok": (0x2D, 0xD4, 0xA8),
        "warn": (0xF5, 0xC4, 0x51),
        "danger": (0xF8, 0x71, 0x71),
        "cover_mode": "solid",
        "title_size": 26,
        "stat_size": 28,
        "slide_bg": (0x0B, 0x12, 0x20),
    },
    "growth_green": {
        "id": "growth_green",
        "name": "增长青绿",
        "desc": "消费 / 医药 / 成长股周会",
        "accent": (0x0F, 0x76, 0x6E),
        "accent2": (0x34, 0xD3, 0x99),
        "panel": (0xF0, 0xFD, 0xF9),
        "ink": (0x13, 0x4E, 0x4A),
        "muted": (0x5E, 0x7A, 0x76),
        "on_dark": (0xFF, 0xFF, 0xFF),
        "on_dark_sub": (0xCB, 0xF5, 0xE8),
        "ok": (0x05, 0x96, 0x69),
        "warn": (0xD9, 0x77, 0x06),
        "danger": (0xDC, 0x26, 0x26),
        "cover_mode": "band",
        "title_size": 26,
        "stat_size": 28,
    },
    "minimal_light": {
        "id": "minimal_light",
        "name": "简约汇报",
        "desc": "内部速读 / 部门周报 / 邮件附件",
        "accent": (0x11, 0x18, 0x27),
        "accent2": (0x25, 0x63, 0xEB),
        "panel": (0xF3, 0xF4, 0xF6),
        "ink": (0x11, 0x18, 0x27),
        "muted": (0x6B, 0x72, 0x80),
        "on_dark": (0xFF, 0xFF, 0xFF),
        "on_dark_sub": (0xE5, 0xE7, 0xEB),
        "ok": (0x05, 0x96, 0x69),
        "warn": (0xD9, 0x77, 0x06),
        "danger": (0xDC, 0x26, 0x26),
        "cover_mode": "minimal",
        "title_size": 28,
        "stat_size": 32,
        "slide_bg": (0xFF, 0xFF, 0xFF),
    },
    "invest_memo": {
        "id": "invest_memo",
        "name": "投研备忘录",
        "desc": "卖方/买方深度备忘录风格",
        "accent": (0x7C, 0x2D, 0x12),
        "accent2": (0xB4, 0x53, 0x09),
        "panel": (0xFA, 0xF7, 0xF2),
        "ink": (0x1C, 0x19, 0x17),
        "muted": (0x78, 0x71, 0x6C),
        "on_dark": (0xFF, 0xFF, 0xFF),
        "on_dark_sub": (0xFE, 0xF3, 0xC7),
        "ok": (0x04, 0x78, 0x57),
        "warn": (0xB4, 0x53, 0x09),
        "danger": (0xB9, 0x1C, 0x1C),
        "cover_mode": "band",
        "title_size": 25,
        "stat_size": 28,
    },
}

DEFAULT_PPT_TEMPLATE = "navy_business"


def list_ppt_templates() -> list:
    return [
        {"id": t["id"], "name": t["name"], "desc": t["desc"], "cover": t["cover_mode"]}
        for t in PPT_TEMPLATES.values()
    ]


def resolve_theme(name: str | None) -> dict:
    key = (name or "").strip().lower()
    # 中文名别名
    alias = {
        "商务深蓝": "navy_business", "深蓝": "navy_business",
        "科技深色": "dark_tech", "深色": "dark_tech", "科技": "dark_tech",
        "增长青绿": "growth_green", "绿色": "growth_green", "青绿": "growth_green",
        "简约汇报": "minimal_light", "简约": "minimal_light", "白底": "minimal_light",
        "投研备忘录": "invest_memo", "备忘录": "invest_memo", "投研": "invest_memo",
    }
    key = alias.get(name or "", key)
    return PPT_TEMPLATES.get(key) or PPT_TEMPLATES[DEFAULT_PPT_TEMPLATE]

