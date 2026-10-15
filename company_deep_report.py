#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
company_deep_report.py — 企业深度分析报告导出（Word / PPT 入口）

输入：前端「企业深析」localStorage 状态 JSON
输出：docx / pptx bytes，供 Flask 直接下载。

模块拆分（2026-10 拆分重构）：
  - company_deep_metrics.py  常量/格式化/指标推导（docx 与 pptx 共用）
  - company_deep_ppt_kit.py  PPT 绘制组件库
  - company_deep_themes.py   PPT 模板主题
  - company_deep_ppt.py      PPT 版式编排（generate_pptx）
  - 本模块                   Word（generate_docx）+ 对外 re-export

设计原则：
- CJK 字体写入 a:latin + a:ea + a:cs，避免中文豆腐块
- Word 用命名样式（Title / Heading / Normal），可出目录
- PPT 一屏一结论，标题是论点不是标签；尽量用统计卡/表格/对比，少堆 bullet
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from typing import Any
from pathlib import Path

try:
    from core_config import OUT_DIR  # 统一入口，避免多处定义分叉
except ImportError:
    OUT_DIR = Path(__file__).parent / "output"  # core_config 不可用时兜底
OUT_DIR.mkdir(exist_ok=True)

from company_deep_metrics import (  # noqa: F401,E402
    FONT, ACCENT, ACCENT2, OK, WARN, DANGER, INK, MUTED, CREAM,
    _s, _n, _fmt, derive_metrics, build_verdict_lines,
)
from company_deep_rules import moat_avg as _moat_avg  # noqa: F401,E402  报告层均分（0 不得当 5）
from company_deep_ppt import (  # noqa: F401,E402
    generate_pptx,
    PPT_TEMPLATES,
    DEFAULT_PPT_TEMPLATE,
    list_ppt_templates,
    resolve_theme,
)

def _set_run_font(run, size=None, bold=None, color=None):
    run.font.name = FONT
    if size is not None:
        run.font.size = size
    if bold is not None:
        run.font.bold = bold
    if color is not None:
        run.font.color.rgb = color
    # East-Asian slot
    try:
        from docx.oxml.ns import qn
        rPr = run._element.get_or_add_rPr()
        rFonts = rPr.get_or_add_rFonts()
        rFonts.set(qn("w:ascii"), FONT)
        rFonts.set(qn("w:hAnsi"), FONT)
        rFonts.set(qn("w:eastAsia"), FONT)
    except Exception:
        pass


def _p(doc, text, style=None, size=None, bold=None, color=None):
    p = doc.add_paragraph(style=style) if style else doc.add_paragraph()
    run = p.add_run(text)
    _set_run_font(run, size=size, bold=bold, color=color)
    return p


def _kv_table(doc, rows):
    from docx.shared import Pt, RGBColor
    table = doc.add_table(rows=0, cols=2)
    try:
        table.style = "Light Grid Accent 1"
    except Exception:
        pass
    for k, v in rows:
        if v is None or v == "":
            continue
        cells = table.add_row().cells
        cells[0].text = str(k)
        cells[1].text = str(v)
        for cell in cells:
            for para in cell.paragraphs:
                for run in para.runs:
                    _set_run_font(run, size=Pt(10))
    return table


def generate_docx(state: dict) -> bytes:
    from docx import Document
    from docx.shared import Pt, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    met = derive_metrics(state)
    verdicts = build_verdict_lines(state, met)
    c = state.get("company") or {}
    name = _s(c.get("name"), "未命名企业")

    doc = Document()
    # A4
    for sec in doc.sections:
        sec.page_width = Cm(21.0)
        sec.page_height = Cm(29.7)
        sec.top_margin = Cm(2.54)
        sec.bottom_margin = Cm(2.54)
        sec.left_margin = Cm(3.0)
        sec.right_margin = Cm(3.0)

    # 标题
    t = doc.add_heading(f"{name} · 企业深度分析报告", level=0)
    for run in t.runs:
        _set_run_font(run, size=Pt(22), bold=True, color=RGBColor(*ACCENT))

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.LEFT
    r = meta.add_run(
        f"代码 {_s(c.get('code'))}  ·  行业 {_s(c.get('industry'))}  ·  市场 {_s(c.get('market'))}  ·  "
        f"分析日期 {_s(c.get('date'))}  ·  分析人 {_s(c.get('analyst'))}"
    )
    _set_run_font(r, size=Pt(10), color=RGBColor(*MUTED))

    if c.get("oneliner"):
        _p(doc, f"一句话画像：{c['oneliner']}", size=Pt(11), bold=True)

    _p(doc, "本报告基于「信息收集 → 业务理解 → 商业模式 → 财务验证 → 风险排雷 → 结论输出」漏斗框架生成，仅供研究参考，不构成投资建议。",
       size=Pt(9), color=RGBColor(*MUTED))

    # 执行摘要 · 一页纸判断卡
    doc.add_heading("执行摘要 · 一页纸判断卡", level=1)
    for q, a, ev in verdicts:
        p = doc.add_paragraph()
        rq = p.add_run(q + "  ")
        _set_run_font(rq, size=Pt(11), bold=True, color=RGBColor(*ACCENT))
        ra = p.add_run(a)
        _set_run_font(ra, size=Pt(11))
        _p(doc, f"证据：{ev}", size=Pt(9), color=RGBColor(*MUTED))

    # 一、公司档案
    doc.add_heading("一、公司档案", level=1)
    _kv_table(doc, [
        ("公司名称", c.get("name")), ("股票代码", c.get("code")),
        ("所属行业", c.get("industry")), ("市场", c.get("market")),
        ("分析日期", c.get("date")), ("分析人", c.get("analyst")),
        ("业务一句话", c.get("oneliner")),
    ])

    # 二、信息收集
    doc.add_heading("二、信息收集", level=1)
    src = state.get("sources") or {}
    notes = [
        ("招股说明书摘录", src.get("ipo")),
        ("年报 MD&A 摘录", src.get("mda")),
        ("审计 / 监管问询", src.get("audit")),
        ("研报观点", src.get("research")),
        ("信源检索摘录", src.get("web")),
    ]
    for title, body in notes:
        if body:
            doc.add_heading(title, level=2)
            _p(doc, str(body), size=Pt(10.5))

    # 三、业务与行业
    doc.add_heading("三、业务与行业", level=1)
    b = state.get("business") or {}
    _kv_table(doc, [
        ("行业规模（亿元）", b.get("size")), ("行业增速（%）", b.get("growth")),
        ("公司市占率（%）", b.get("share")), ("生命周期", b.get("lifecycle")),
        ("CR3 / CR5（%）", f"{_s(b.get('cr3'))} / {_s(b.get('cr5'))}"),
        ("政策方向", b.get("policy")), ("行业评分", b.get("score")),
    ])
    for title, key in [("靠什么赚钱", "what"), ("行业天花板", "ceiling"),
                       ("关键成功要素", "kfs"), ("行业位置", "position")]:
        if b.get(key):
            doc.add_heading(title, level=2)
            _p(doc, str(b[key]), size=Pt(10.5))

    # 四、商业模式
    doc.add_heading("四、商业模式拆解", level=1)
    model = state.get("model") or {}
    doc.add_heading("商业闭环", level=2)
    _kv_table(doc, [
        ("① 客户是谁", model.get("customer")), ("② 价值主张", model.get("value")),
        ("③ 渠道触达", model.get("channel")), ("④ 收入结构", model.get("revenue")),
        ("⑤ 成本结构", model.get("cost")), ("⑥ 利润与回款", model.get("cash")),
        ("最脆弱一环", model.get("weakest")),
    ])
    _kv_table(doc, [
        ("收入拆分", model.get("revMix")),
        ("第一大 / 前五大客户（%）", f"{_s(model.get('cust1'))} / {_s(model.get('cust5'))}"),
        ("回款模式", model.get("payment")), ("定价权", model.get("pricing")),
        ("利润公式", model.get("formula")),
        ("毛利率 / 净利率（%）", f"{_s(model.get('gm'))} / {_s(model.get('nm'))}"),
        ("商业模式类型", model.get("bizType")), ("模式评分", model.get("modelScore")),
    ])

    # 五、竞争壁垒
    doc.add_heading("五、竞争壁垒（护城河）", level=1)
    m = state.get("moat") or {}
    _kv_table(doc, [
        ("技术/专利", m.get("tech")), ("品牌心智", m.get("brand")),
        ("渠道网络", m.get("channel")), ("成本/规模", m.get("cost")),
        ("网络效应", m.get("network")), ("牌照/资质", m.get("license")),
        ("六维均分", f"{_fmt(met['moat_avg'])}/10"),
        ("市占率变化", m.get("shareTrend")), ("毛利率 vs 同行", m.get("marginPeers")),
        ("客户为什么选它", m.get("whyChoose")), ("对手为何难进入", m.get("whyBlocked")),
        ("壁垒可持续性", m.get("dur")),
    ])

    # 六、财务验证
    doc.add_heading("六、财务验证", level=1)
    f = state.get("finance") or {}
    _kv_table(doc, [
        ("营业收入（亿元）", f.get("rev")), ("净利润（亿元）", f.get("np")),
        ("毛利率", _fmt(met["gm"], 1, "%")), ("净利率", _fmt(met["nm"], 1, "%")),
        ("ROE（%）", f.get("roe")), ("经营现金流（亿元）", f.get("ocf")),
        ("OCF / 净利润", _fmt(met["ocf_np"], 2)),
        ("资产负债率", _fmt(met["debt_ratio"], 1, "%")),
        ("货币资金/有息负债", _fmt(met["cash_debt"], 2) if met["cash_debt"] is not None else "—"),
        ("财务健康分", f"{met['health']}/100"),
    ])
    doc.add_heading("杜邦拆解", level=2)
    _p(doc,
       f"ROE ≈ {_fmt(met['du_roe'])}% = 净利率 {_fmt(met['nm'])}% × 周转率 {_fmt(met['tat'], 2)} × 权益乘数 {_fmt(met['em'], 2)}",
       size=Pt(11), bold=True)
    if met["alerts"]:
        doc.add_heading("现金流照妖镜 · 自动排雷", level=2)
        for title, desc in met["alerts"]:
            _p(doc, f"⚠ {title}：{desc}", size=Pt(10.5))

    # 七、治理与风险
    doc.add_heading("七、治理与风险", level=1)
    g = state.get("gov") or {}
    _kv_table(doc, [
        ("实际控制人", g.get("controller")), ("持股比例（%）", g.get("hold")),
        ("质押率（%）", g.get("pledge")), ("股权结构", g.get("structure")),
        ("高管稳定性", g.get("stable")), ("关联交易", g.get("related")),
        ("分红/回购", g.get("dividend")), ("承诺兑现", g.get("promise")),
        ("激励机制", g.get("incentive")), ("治理评分", g.get("score")),
    ])
    risks = state.get("risks") or []
    if risks:
        doc.add_heading("风险因素登记", level=2)
        table = doc.add_table(rows=1, cols=3)
        try:
            table.style = "Light Grid Accent 1"
        except Exception:
            pass
        for i, h in enumerate(["类型", "严重度", "描述"]):
            table.rows[0].cells[i].text = h
        for r in risks:
            cells = table.add_row().cells
            cells[0].text = _s(r.get("type"))
            cells[1].text = _s(r.get("level"))
            cells[2].text = _s(r.get("text"))

    red = state.get("red") or {}
    if red.get("verdict") or red.get("worst"):
        doc.add_heading("排雷结论", level=2)
        _kv_table(doc, [("排雷结论", red.get("verdict")), ("最担心的一颗雷", red.get("worst"))])

    # 八、估值
    doc.add_heading("八、估值判断", level=1)
    v = state.get("val") or {}
    _kv_table(doc, [
        ("PE（TTM）", v.get("pe")), ("历史 PE 分位（%）", v.get("pePct")),
        ("PB", v.get("pb")), ("历史 PB 分位（%）", v.get("pbPct")),
        ("PS", v.get("ps")), ("同行 PE 中位", v.get("peerPe")),
        ("DCF FCF / g / WACC", f"{_s(v.get('dcfFcf'))} / {_s(v.get('dcfG'))}% / {_s(v.get('dcfWacc'))}%"),
        ("价格结论", v.get("verdict")),
    ])
    if v.get("reason"):
        _p(doc, f"估值逻辑：{v['reason']}", size=Pt(10.5))

    # 九、竞争推演
    pb = state.get("pb") or {}
    if any(pb.get(k) for k in ("strength", "weak", "rival", "conclusion")) or pb.get("steps"):
        doc.add_heading("九、竞争推演", level=1)
        _kv_table(doc, [
            ("我方优势", pb.get("strength")), ("我方软肋", pb.get("weak")),
            ("对手与武器", pb.get("rival")), ("对手打法", pb.get("move")),
            ("冲击评估", pb.get("impact")), ("守不守得住", pb.get("conclusion")),
            ("投资含义", pb.get("invest")),
        ])
        if pb.get("steps"):
            doc.add_heading("推演步骤", level=2)
            for i, step in enumerate(pb["steps"], 1):
                _p(doc, f"{i}. {step}", size=Pt(10.5))

    # 十、结论
    doc.add_heading("十、最终结论", level=1)
    for q, a, _ev in verdicts:
        _p(doc, f"{q}  {a}", size=Pt(11))

    _p(doc, "— 本报告由 data.tool 企业深析模块自动生成 —", size=Pt(9), color=RGBColor(*MUTED))

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


# ── PowerPoint ────────────────────────────────────────


def safe_filename(name: str, ext: str) -> str:
    import re
    base = re.sub(r'[\\/:*?"<>|\s]+', "_", (name or "企业深度分析").strip())[:60]
    return f"{base}_深度分析报告.{ext}"
