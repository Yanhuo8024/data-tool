# -*- coding: utf-8 -*-
"""
skill_manager.py — AI 助手 Skill 管理系统
- Skill = 一个目录（skills/<name>/），含 SKILL.md（YAML frontmatter + 正文系统提示）
- 支持导入（粘贴 SKILL.md 文本 / 上传 zip 压缩包）、列表、启停、删除
- 已启用的 Skill 正文会注入 AI Agent 的系统提示，增强助手能力
- 安全：仅解析文本，不执行任何脚本
"""
import os
import re
import io
import json
import shutil
import zipfile
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(BASE_DIR, "skills")
os.makedirs(SKILLS_DIR, exist_ok=True)


# ==================== SKILL.md 解析 ====================

def _parse_skill_md(text: str):
    """解析 SKILL.md：frontmatter（--- name/description ---）+ 正文。返回 (meta, body)"""
    meta = {}
    body = text
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', text, re.S)
    if m:
        fm, body = m.group(1), m.group(2)
        lines = fm.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            if ':' in line:
                k, v = line.split(':', 1)
                key = k.strip().lower()
                val = v.strip().strip('"\'')
                # 支持 YAML 多行折叠块（description: > / >-）与字面块（description: | / |-）
                if val in ('>', '|', '>-', '|-'):
                    collected = []
                    j = i + 1
                    while j < len(lines) and lines[j].startswith(('  ', '\t')):
                        collected.append(lines[j].strip())
                        j += 1
                    val = ' '.join(collected)
                    i = j
                meta[key] = val
            i += 1
    name = re.sub(r'[^\w\u4e00-\u9fa5-]+', '-', (meta.get('name') or 'skill').strip()).strip('-') or 'skill'
    desc = meta.get('description', '') or body.strip().splitlines()[0][:60] if body.strip() else ''
    return name, desc, body.strip()


def _safe_skill_name(name: str) -> str:
    return re.sub(r'[^\w\u4e00-\u9fa5-]+', '-', name).strip('-') or 'skill'


# ==================== 列表 / 启停 ====================

def list_skills():
    """列出所有 Skill：name/description/enabled/created/items"""
    out = []
    if not os.path.isdir(SKILLS_DIR):
        return out
    for name in sorted(os.listdir(SKILLS_DIR)):
        d = os.path.join(SKILLS_DIR, name)
        md = os.path.join(d, 'SKILL.md')
        if not os.path.isdir(d) or not os.path.isfile(md):
            continue
        try:
            with open(md, 'r', encoding='utf-8') as f:
                text = f.read()
            _, desc, body = _parse_skill_md(text)
            enabled = os.path.isfile(os.path.join(d, '.enabled'))
            created = ''
            try:
                with open(os.path.join(d, '.meta.json'), 'r', encoding='utf-8') as f:
                    created = json.load(f).get('created', '')
            except Exception:
                created = datetime.fromtimestamp(os.path.getmtime(md)).strftime('%Y-%m-%d %H:%M')
            items = []
            for root, dirs, files in os.walk(d):
                for fn in files:
                    if fn not in ('SKILL.md', '.enabled', '.meta.json'):
                        items.append(os.path.relpath(os.path.join(root, fn), d).replace('\\', '/'))
            out.append({'name': name, 'description': desc, 'enabled': enabled,
                        'created': created, 'items': items, 'body_len': len(body)})
        except Exception as e:
            logger.warning("读取 skill %s 失败: %s", name, e)
    return out


def _read_skill(sname: str):
    """读取单个 Skill 的 (name, desc, body)，不存在/出错返回 None"""
    md = os.path.join(SKILLS_DIR, _safe_skill_name(sname), 'SKILL.md')
    if not os.path.isfile(md):
        return None
    try:
        with open(md, 'r', encoding='utf-8') as f:
            _, desc, body = _parse_skill_md(f.read())
        return _safe_skill_name(sname), desc or '', body or ''
    except Exception:
        return None


def get_skill(sname: str):
    """获取单个 Skill 的完整信息（名称/描述/正文），供 run_skill 工具调用"""
    got = _read_skill(sname)
    if not got:
        return None
    name, desc, body = got
    return {'name': name, 'description': desc, 'body': body}


def get_skills_index() -> list:
    """已启用 Skill 的轻量索引（仅名称+描述，不包含正文），供 Agent 决定是否调用"""
    out = []
    for s in list_skills():
        if not s['enabled']:
            continue
        out.append({'name': s['name'], 'description': s['description'], 'body_len': s['body_len']})
    return out


def get_skills_prompt(active_call: bool = False) -> str:
    """已启用 Skill 的提示文本（注入系统提示）。
    仅含技能名称+描述索引，正文通过 run_skill 工具按需加载，省 token。
    active_call=True：面向 Agent（有工具接口），追加"必须调用 run_skill 深度分析"指令；
    active_call=False：普通直连 chat 路径，仅作为被动参考注入。"""
    idx = get_skills_index()
    if not idx:
        return ''
    bullets = "\n".join(f"- `{s['name']}`：{s['description']}" for s in idx)
    head = "# 可用 Skills（专家深度分析工作流）\n" + bullets
    if active_call:
        head += (
            "\n\n当用户请求深度/专业分析且命中上述某个技能时，必须通过工具 `run_skill` 主动加载该技能并严格遵循其工作流进行分析。"
            "不要凭记忆臆断技能内容，也不要在未调用技能时假装已按技能工作。直接用正文完成的浅答案不算完成深度分析。"
        )
    return head


# ==================== 技能自动匹配（Trae Work 风格自动触发） ====================
# 不依赖 LLM 自觉调用 run_skill：在用户请求进入 Agent 前，先用规则判断是否命中某个已启用
# 技能，命中则直接加载技能正文注入上下文，让系统提示具备该技能的完整工作流。

# 技能触发关键词（与技能描述意图匹配），用于"自动触发"：
#   key : 技能名
#   val : 触发词列表（命中任一即候选该技能）
_SKILL_TRIGGERS = {
    "buffett-oracle-analyzer": ["贵州茅台", "巴菲特", "护城河", "商业模式", "绝对投资", "估值模型",
                                 "目标价", "投资机会", "基本面深度", "股票分析", "买点", "卖点",
                                 "值得投资", "股票代码", "财报深度", "管理层", "roe 背后的生意"],
    "finance-sentiment": ["sentiment", "social", "reddit", "polymarket", "buzz", "ticker heat",
                          "市场情绪", "热度", "社交情绪", "股市情绪", "多空情绪", "明星股"],
    "company-analysis": ["全方位分析", "全面分析", "公司分析", "公司研报", "分析报告", "上市公司分析",
                         "基本面分析", "公司概况", "竞争分析", "swot", "swot分析", "公司风险",
                         "分析一下", "分析下", "看看这家公司", "这家公司怎么样", "公司投资价值"],
    "industry-research": ["行业研究", "行业报告", "行业全景", "行业深度", "研究一下这个行业",
                          "市场规模", "产业链", "竞争格局", "宏观政策", "行业趋势", "行业机会",
                          "行业风险", "赛道研究", "行业概览", "行业分析", "看看这个行业", "xx行业",
                          "行业前景", "行业现状", "行业space", "行业龙头", "产业研究", "垂直行业"],
}


def _detect_company_ref(text: str) -> bool:
    """消息中是否含可识别公司引用（股票代码 6位A股/HK+4位，或已知公司名），
    用于 company-analysis 技能的兜底自动触发。覆盖"帮我分析腾讯/宁德时代怎么样/看下比亚迪/
    分析600519"等不命中固定短语的自然问法，确保识别到公司即可自动启用该技能。"""
    if not text:
        return False
    if re.search(r'(?<!\d)(\d{6})(?!\d)', text) or re.search(r'hk\s?\d{4}', text):
        return True
    try:
        from company_analysis import STOCK_ALIASES
    except Exception:
        return False
    return any(k in text for k in STOCK_ALIASES)


def match_skill(text: str) -> dict:
    """
    自动匹配已启用的 Skill。返回：
      {matched: bool, name, description, body, score}
    未匹配或未启用返回 matched=False。
    说明：优先匹配已启用技能；若仅有未启用技能命中，匹配结果 matched=False 但返回 candidate，
    便于上层提醒"有技能未启用"。
    """
    if not text:
        return {"matched": False, "name": None, "description": "", "body": "", "score": 0}
    low = text.lower()
    # 先看未启用候选（用于提示），再匹配已启用
    enabled = {s['name'] for s in get_skills_index()}
    best = None
    best_score = 0
    for name, kws in _SKILL_TRIGGERS.items():
        score = sum(1 for kw in kws if kw.lower() in low)
        # 消歧（2026-09-22）：公司名会同时命中 buffett（茅台/巴菲特词条）与 company。
        # 明确「全方位/公司分析/公司研报」→ company-analysis；明确「巴菲特/护城河」→ buffett。
        if '全方位' in low or '公司分析' in low or '公司研报' in low or '基本面分析' in low:
            if name == 'company-analysis':
                score += 3
        if '巴菲特' in low or '护城河' in low or '护城河分析' in low:
            if name == 'buffett-oracle-analyzer':
                score += 3
        if any(x in low for x in ('行业研究', '行业全景', '行业报告', '产业链', '竞争格局')):
            if name == 'industry-research':
                score += 3
        if score > best_score:
            best_score = score
            best = name
    # 行业研究动态触发：形如"X行业/X产业/X赛道/X板块/X报告 [+ 研究/前景类词]" → industry-research
    # 覆盖任意具体行业名（半导体行业、新能源汽车板块、医药赛道），无需枚举行业名
    # 该模式优先于 company-analysis：即使命中"分析一下"等通用词，只要前序有行业名归行业研究
    _industry_neg = ("不喜欢", "喜欢", "讨厌", "爱好", "无关", "关于行业", "一个行业",
                     "是什么", "定义", "概念", "什么叫", "规范", "含义")
    for suffix in ("行业", "产业", "赛道", "板块", "赛道研究", "赛道分析"):
        idx = low.find(suffix)
        if idx > 0:
            prefix = low[:idx].strip().replace("帮我", "").replace("请", "").replace("分析", "")
            tail = low[idx + len(suffix):]
            if not prefix:
                continue
            if any(neg in low for neg in _industry_neg):
                continue
            # 后缀紧跟概念解释场景（"行业是什么/行业规范/行业定义"）→ 知识问答，非行业研究
            if any(neg in tail for neg in ("是什么", "定义", "概念", "规范", "含义")):
                continue
            if prefix and len(prefix) >= 2 and len(prefix) <= 8:
                if best_score <= 1 or best == "company-analysis":
                    best = "industry-research"
                    best_score = 2
                break
    # "X研究报告" / "X行业报告" 形态（如 新能源汽车研究报告、医药行业报告）
    if best_score <= 1:
        for suffix in ("研究报告", "行业报告"):
            idx = low.find(suffix)
            if idx > 0:
                prefix = low[:idx].strip().replace("做一份", "").replace("帮我", "").replace("请", "")
                if prefix and len(prefix) >= 2 and len(prefix) <= 8 and "研究" not in prefix:
                    best = "industry-research"
                    best_score = 2
                    break
    # 兜底：上述关键词/行业正则均未命中时，若消息提及股票代码或已知公司名 → 自动激活
    # company-analysis。不覆盖已强命中的 industry-research 等其他技能。
    if (best_score <= 0 or best is None) and _detect_company_ref(low):
        best = "company-analysis"
        best_score = 1
    if best_score <= 0 or best is None:
        return {"matched": False, "name": None, "description": "", "body": "", "score": 0}
    got = get_skill(best)
    if not got:
        return {"matched": False, "name": best, "description": "", "body": "", "score": best_score}
    if best not in enabled:
        return {"matched": False, "name": best, "description": got.get('description', ''),
                "body": "", "score": best_score}
    return {"matched": True, "name": best,
            "description": got.get('description', ''),
            "body": got.get('body', ''), "score": best_score}


# ==================== 导入 / 删除 ====================

def import_skill_from_md(content: str, name: str = None) -> dict:
    """从 SKILL.md 文本导入。name 缺省时取 frontmatter 名称"""
    if not content or not content.strip():
        return {"ok": False, "error": "内容为空"}
    skill_name, desc, body = _parse_skill_md(content)
    if name and name.strip():
        skill_name = _safe_skill_name(name)
    if not body:
        return {"ok": False, "error": "SKILL.md 缺少正文"}
    d = os.path.join(SKILLS_DIR, skill_name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, 'SKILL.md'), 'w', encoding='utf-8') as f:
        f.write(content)
    with open(os.path.join(d, '.meta.json'), 'w', encoding='utf-8') as f:
        json.dump({'created': datetime.now().strftime('%Y-%m-%d %H:%M'), 'desc': desc}, f, ensure_ascii=False)
    return {"ok": True, "name": skill_name, "description": desc, "enabled": False}


def import_skill_from_zip(zip_bytes: bytes) -> dict:
    """从 zip 压缩包导入（安全：仅提取 SKILL.md，拒绝路径穿越）"""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return {"ok": False, "error": "无效的 zip 文件"}
    md_entry = None
    for info in zf.infolist():
        # 防路径穿越
        if info.filename.startswith('/') or '..' in info.filename:
            return {"ok": False, "error": f"压缩包包含不安全路径: {info.filename}"}
        if info.filename.lower().endswith('skill.md'):
            md_entry = info.filename
            break
    if not md_entry:
        return {"ok": False, "error": "压缩包中未找到 SKILL.md"}
    content = zf.read(md_entry).decode('utf-8', errors='replace')
    return import_skill_from_md(content)


def set_skill_enabled(name: str, enabled: bool) -> dict:
    d = os.path.join(SKILLS_DIR, _safe_skill_name(name))
    if not os.path.isfile(os.path.join(d, 'SKILL.md')):
        return {"ok": False, "error": f"Skill 不存在: {name}"}
    flag = os.path.join(d, '.enabled')
    if enabled:
        open(flag, 'w').close()
    else:
        if os.path.isfile(flag):
            os.remove(flag)
    return {"ok": True, "name": name, "enabled": enabled}


def delete_skill(name: str) -> dict:
    d = os.path.join(SKILLS_DIR, _safe_skill_name(name))
    if not os.path.isdir(d):
        return {"ok": False, "error": f"Skill 不存在: {name}"}
    shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "name": name}
