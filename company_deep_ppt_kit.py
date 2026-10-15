#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
company_deep_ppt_kit.py — PPT 绘制组件库（从 company_deep_report 拆出）

文本/形状/渐变/透明度 + 内容页框架 + KPI 卡/标签/徽章/条形/警示行/文案卡。
generate_pptx（company_deep_ppt）只做版式编排，绘制细节全在本模块。
"""
from datetime import datetime

from company_deep_metrics import (
    FONT, ACCENT, ACCENT2, OK, WARN, DANGER, INK, MUTED, CREAM,
)
# 🚨 必需补入：本模块在 `_slide_title` / 版式框架等处使用 `PPT_TEMPLATES[DEFAULT_PPT_TEMPLATE]`，
#    但原实现从未导入这两个名字 → 只要走到那三行就 `NameError`（架构守卫 R7 抓到）。
#    `company_deep_themes` 是无依赖叶子模块，导入它不会成环。
from company_deep_themes import PPT_TEMPLATES, DEFAULT_PPT_TEMPLATE  # noqa: F401

def _set_cjk_font(run, font_name=FONT):
    """pptx-official: set a:latin + a:ea + a:cs."""
    from pptx.oxml.ns import qn

    run.font.name = font_name
    rPr = run._r.get_or_add_rPr()
    successors = {
        "a:ea": ("a:cs", "a:sym", "a:hlinkClick", "a:hlinkMouseOver", "a:rtl", "a:extLst"),
        "a:cs": ("a:sym", "a:hlinkClick", "a:hlinkMouseOver", "a:rtl", "a:extLst"),
    }
    for tag in ("a:ea", "a:cs"):
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.insert_element_before(el, *successors[tag])
        el.set("typeface", font_name)


def _add_text(slide, x, y, w, h, text, size=16, bold=False, color=INK, align=None, wrap=True,
              line_spacing=1.0, space_after=0, font=FONT):
    """文本框：多行、CJK 字体、行距/段距可控。"""
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_AUTO_SIZE

    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = wrap
    try:
        tf.auto_size = MSO_AUTO_SIZE.NONE
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    except Exception:
        pass
    lines = text.split("\n") if isinstance(text, str) else list(text)
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line
        if align is not None:
            p.alignment = align
        if line_spacing and line_spacing != 1.0:
            p.line_spacing = line_spacing
        if space_after:
            p.space_after = Pt(space_after)
        if p.runs:
            p.runs[0].font.size = Pt(size)
            p.runs[0].font.bold = bold
            p.runs[0].font.color.rgb = RGBColor(*color)
            _set_cjk_font(p.runs[0], font)
    return box


def _add_shape(slide, kind, x, y, w, h, fill, line=None, line_w=0.75, adj=None):
    """通用形状：kind 为 MSO_SHAPE 成员名（字符串）。"""
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.dml.color import RGBColor

    shape = slide.shapes.add_shape(getattr(MSO_SHAPE, kind), Inches(x), Inches(y), Inches(w), Inches(h))
    shape.shadow.inherit = False
    if fill is None:
        shape.fill.background()
    else:
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(*fill)
    if line:
        shape.line.color.rgb = RGBColor(*line)
        shape.line.width = Pt(line_w)
    else:
        shape.line.fill.background()
    if adj is not None:
        try:
            shape.adjustments[0] = adj
        except Exception:
            pass
    tf = shape.text_frame
    try:
        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
    except Exception:
        pass
    return shape


def _add_rect(slide, x, y, w, h, fill, line=None):
    return _add_shape(slide, "ROUNDED_RECTANGLE", x, y, w, h, fill, line, adj=0.08)


def _set_alpha(shape, opacity_pct):
    """给纯色填充加透明度：opacity_pct = 不透明度 0-100。"""
    from pptx.oxml.ns import qn
    try:
        sF = shape.fill._xPr.find(qn('a:solidFill'))
        clr = sF.find(qn('a:srgbClr'))
        for a in clr.findall(qn('a:alpha')):
            clr.remove(a)
        el = clr.makeelement(qn('a:alpha'), {'val': str(int(opacity_pct * 1000))})
        clr.append(el)
    except Exception:
        pass


def _gradient_rect(slide, x, y, w, h, c1, c2, angle=115):
    """线性渐变矩形（角度：0=左→右，90=上→下）。"""
    from pptx.util import Inches
    from pptx.dml.color import RGBColor
    shape = slide.shapes.add_shape(1, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.shadow.inherit = False
    shape.line.fill.background()
    try:
        shape.fill.gradient()
        stops = shape.fill.gradient_stops
        stops[0].color.rgb = RGBColor(*c1)
        stops[0].position = 0.0
        stops[1].color.rgb = RGBColor(*c2)
        stops[1].position = 1.0
        try:
            shape.fill.gradient_angle = angle
        except Exception:
            pass
    except Exception:
        shape.fill.solid()
        shape.fill.fore_color.rgb = RGBColor(*c1)
    try:
        shape.text_frame.margin_left = shape.text_frame.margin_right = \
            shape.text_frame.margin_top = shape.text_frame.margin_bottom = 0
    except Exception:
        pass
    return shape


# ── 色彩派生 ──
def _mix(c1, c2, t):
    """c1 向 c2 混合 t∈[0,1]"""
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


def _soften(th, t=0.86):
    """标签/淡字底色：浅主题用强调色淡化，深主题用第二强调色淡化。"""
    bg = th.get("slide_bg") or (0xFF, 0xFF, 0xFF)
    base = th["accent2"] if _is_dark(th) else th["accent"]
    return _mix(base, bg, t)


def _faint(th, t=0.90):
    """极淡分隔线色。"""
    bg = th.get("slide_bg") or (0xFF, 0xFF, 0xFF)
    return _mix(th["ink"], bg, t)


def _is_dark(th):
    bg = th.get("slide_bg") or (0xFF, 0xFF, 0xFF)
    return sum(bg) < 390


def _card_color(th):
    return th.get("card") or _mix(th.get("slide_bg") or (0xFF, 0xFF, 0xFF), th["ink"], 0.04)


def _hi(th, color):
    """对比度保障：深底主题下，颜色若与背景过近则向白色提亮。"""
    bg = th.get("slide_bg") or (0xFF, 0xFF, 0xFF)
    if _is_dark(th) and sum(abs(a - b) for a, b in zip(color, bg)) < 170:
        return _mix(color, (0xFF, 0xFF, 0xFF), 0.62)
    return color


# ── 版式框架 ──
def _slide_title(slide, title, subtitle=None, theme=None):
    """旧版兼容钩子（generate_pptx 内已用 _content_chrome 取代）。"""
    th = theme or PPT_TEMPLATES[DEFAULT_PPT_TEMPLATE]
    _add_text(slide, 0.55, 0.28, 12.2, 0.55, title, size=th.get("title_size", 26), bold=True, color=th["ink"])
    if subtitle:
        _add_text(slide, 0.55, 0.85, 12.2, 0.35, subtitle, size=12, color=th["muted"])


def _content_chrome(prs, th, num, title, sub, company_tag, page_no):
    """内容页框架：背景 + 章节号 + 标题/强调条/副标题 + 右上公司标 + 页脚。"""
    from pptx.enum.text import PP_ALIGN
    s = prs.slides.add_slide(prs.slide_layouts[6])
    bg = th.get("slide_bg") or (0xFF, 0xFF, 0xFF)
    _add_shape(s, "RECTANGLE", 0, 0, 13.333, 7.5, bg)
    faint_num = _soften(th, 0.80 if _is_dark(th) else 0.82)
    # 章节号 + 标题
    _add_text(s, 0.55, 0.34, 1.15, 0.62, f"{int(num):02d}", size=30, bold=True, color=faint_num)
    _add_text(s, 1.42, 0.30, 8.6, 0.5, title, size=22, bold=True, color=th["ink"])
    _add_shape(s, "ROUNDED_RECTANGLE", 1.44, 0.86, 0.52, 0.045, th["accent2"], adj=0.5)
    if sub:
        _add_text(s, 2.06, 0.80, 8.0, 0.3, sub, size=11, color=th["muted"])
    if company_tag:
        _add_text(s, 8.8, 0.38, 3.98, 0.3, company_tag, size=10, color=th["muted"],
                  align=PP_ALIGN.RIGHT)
    # 页脚
    _add_shape(s, "RECTANGLE", 0.55, 7.06, 12.23, 0.012, _faint(th, 0.92 if _is_dark(th) else 0.88))
    foot = f"{company_tag}    ·    {datetime.now().strftime('%Y-%m-%d')}" if company_tag else datetime.now().strftime('%Y-%m-%d')
    _add_text(s, 0.55, 7.14, 9.0, 0.26, foot, size=9, color=th["muted"])
    _add_text(s, 11.2, 7.14, 1.58, 0.26, f"PAGE {page_no:02d}", size=9, color=th["muted"],
              align=PP_ALIGN.RIGHT)
    return s


def _stat_card(slide, x, y, w, h, value, label, accent=None, theme=None):
    """KPI 卡：卡片 + 顶部强调条 + 大数字 + 标签。"""
    th = theme or PPT_TEMPLATES[DEFAULT_PPT_TEMPLATE]
    acc = _hi(th, accent or th["accent"])
    card = _card_color(th)
    _add_rect(slide, x, y, w, h, card)
    _add_shape(slide, "ROUNDED_RECTANGLE", x, y, w, 0.055, acc, adj=0.5)
    vs = th.get("stat_size", 28)
    _add_text(slide, x + 0.18, y + 0.22, w - 0.36, h * 0.5, str(value), size=vs, bold=True, color=acc)
    _add_text(slide, x + 0.18, y + h - 0.40, w - 0.36, 0.3, label, size=10.5, color=th["muted"])


def _pill(slide, x, y, text, th, accent=None, w=None, size=10.5):
    """圆角小标签（文字居中；浅底主题文字略加深保证对比度）。"""
    from pptx.enum.text import PP_ALIGN
    acc = accent or th["accent"]
    if not _is_dark(th):
        acc = _mix(acc, (0, 0, 0), 0.22)
    w = w or max(0.9, 0.34 + len(str(text)) * 0.13)
    _add_shape(slide, "ROUNDED_RECTANGLE", x, y, w, 0.34, _soften(th), adj=0.5)
    _add_text(slide, x, y + 0.045, w, 0.26, str(text), size=size, bold=True, color=acc,
              align=PP_ALIGN.CENTER)
    return x + w + 0.14


def _num_badge(slide, x, y, n, th, d=0.42, accent=None):
    """圆形序号徽章。"""
    from pptx.enum.text import PP_ALIGN
    acc = accent or th["accent"]
    _add_shape(slide, "OVAL", x, y, d, d, acc)
    _add_text(slide, x, y + 0.055, d, 0.32, str(n), size=14, bold=True, color=th["on_dark"],
              align=PP_ALIGN.CENTER)
    return x + d


def _bar_row(slide, x, y, w, label, ratio, value_text, th, accent=None, label_w=1.55, bar_h=0.16):
    """横向条形：标签 + 背景条/前景条 + 数值。ratio∈[0,1]。"""
    acc = _hi(th, accent or th["accent"])
    _add_text(slide, x, y - 0.02, label_w, 0.3, label, size=11.5, color=th["ink"])
    tx = x + label_w
    tw = w - label_w - 0.85
    _add_shape(slide, "ROUNDED_RECTANGLE", tx, y + 0.03, tw, bar_h, _faint(th, 0.90), adj=0.5)
    rr = max(0.0, min(1.0, float(ratio or 0)))
    if rr > 0.01:
        _add_shape(slide, "ROUNDED_RECTANGLE", tx, y + 0.03, max(0.08, tw * rr), bar_h, acc, adj=0.5)
    _add_text(slide, x + w - 0.8, y - 0.02, 0.8, 0.3, value_text, size=12, bold=True, color=acc)


def _note_row(slide, x, y, w, title, body, th, accent=None, h=0.62, title_w=None):
    """左色条提示行（排雷信号/风险）。"""
    acc = _hi(th, accent or th["accent"])
    _add_rect(slide, x, y, w, h, _card_color(th))
    _add_shape(slide, "RECTANGLE", x, y, 0.055, h, acc)
    tw = title_w or (2.2 if title else 0)
    if title:
        _add_text(slide, x + 0.18, y + 0.09, tw, 0.3, title, size=11.5, bold=True, color=acc)
    _add_text(slide, x + 0.18 + tw, y + 0.09, w - tw - 0.36, h - 0.14, body, size=11,
              color=th["ink"], line_spacing=1.05)


def _text_card(slide, x, y, w, h, title, body, th, accent=None, body_size=11.5):
    """带标题的文案卡。"""
    acc = _hi(th, accent or th["accent"])
    _add_rect(slide, x, y, w, h, _card_color(th))
    _add_text(slide, x + 0.18, y + 0.12, w - 0.36, 0.3, title, size=11.5, bold=True, color=acc)
    _add_text(slide, x + 0.18, y + 0.46, w - 0.36, h - 0.6, body, size=body_size,
              color=th["ink"], line_spacing=1.12)


def _bullet_block(slide, x, y, w, h, items, size=14, theme=None):
    th = theme or PPT_TEMPLATES[DEFAULT_PPT_TEMPLATE]
    text = "\n".join("• " + i for i in items if i)
    return _add_text(slide, x, y, w, h, text, size=size, color=th["ink"], line_spacing=1.15)


def _paint_slide_bg(slide, prs, color):
    from pptx.util import Inches
    from pptx.dml.color import RGBColor
    bg = slide.shapes.add_shape(1, 0, 0, prs.slide_width, prs.slide_height)
    bg.fill.solid()
    bg.fill.fore_color.rgb = RGBColor(*color)
    bg.line.fill.background()
    return bg


