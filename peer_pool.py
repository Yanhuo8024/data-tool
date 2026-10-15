# -*- coding: utf-8 -*-
"""可比公司同业池：**选错了自动切回正确的池**。

为什么需要
----------
原来的同业池是「按代码硬编码的几组」，覆盖不到的行业就**悄悄退化成空**（或给出跨行业公司）。
做可比估值时，同业选错的后果比"没有同业"更严重——会给出**看起来专业但完全错误**的对比。

做法
----
1. **行业 → 同业池**：按公司画像/名称里的行业关键词匹配到池；
2. **校验已有同业**：如果上游给的同业与识别出的行业不匹配（一个都不在池内），
   判定为「选错」→ **自动切回正确池**，并把原因写清楚；
3. **宁可空也不要错**：行业识别不出时**不猜**，返回空池 + 说明。

零项目依赖（仅标准库）。池内代码格式：A 股 6 位、港股 `HK` + 4/5 位、海外用「交易所:代码」说明。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# ==================== 同业池 ====================

# 每个池：行业名 + 匹配关键词 + 成员（name/code/market/note）
INDUSTRY_POOLS: Dict[str, Dict[str, Any]] = {
    'auto_glass': {
        'name': '汽车玻璃 / 汽车零部件',
        'keywords': ('汽车玻璃', '汽玻', '车用玻璃', '挡风玻璃', '风挡玻璃', '车窗玻璃',
                     '汽车零部件', '零部件', '汽配', '玻璃', 'auto glass', 'autoglass'),
        'peers': [
            {'name': '旭硝子（AGC）', 'code': 'TSE:5201', 'market': 'jp',
             'note': '全球汽车玻璃龙头'},
            {'name': '圣戈班（Saint-Gobain）', 'code': 'EPA:SGO', 'market': 'eu',
             'note': '欧洲建材与汽车玻璃'},
            {'name': '板硝子（NSG）', 'code': 'TSE:5202', 'market': 'jp',
             'note': '汽车玻璃全球主要供应商'},
            {'name': '信义玻璃', 'code': 'HK0868', 'market': 'hk',
             'note': '汽车玻璃 + 浮法玻璃'},
            {'name': '华域汽车', 'code': '600741', 'market': 'a',
             'note': '国内汽车零部件龙头'},
            {'name': '均胜电子', 'code': '600699', 'market': 'a',
             'note': '汽车安全与电子'},
        ],
    },
    'baijiu': {
        'name': '白酒',
        'keywords': ('白酒', '酱香', '浓香', '酿酒', '酒类'),
        'peers': [
            {'name': '贵州茅台', 'code': '600519', 'market': 'a'},
            {'name': '五粮液', 'code': '000858', 'market': 'a'},
            {'name': '泸州老窖', 'code': '000568', 'market': 'a'},
            {'name': '山西汾酒', 'code': '600809', 'market': 'a'},
        ],
    },
    'bank': {
        'name': '银行',
        'keywords': ('银行', '商业银行', 'bank'),
        'peers': [
            {'name': '工商银行', 'code': '601398', 'market': 'a'},
            {'name': '建设银行', 'code': '601939', 'market': 'a'},
            {'name': '招商银行', 'code': '600036', 'market': 'a'},
            {'name': '平安银行', 'code': '000001', 'market': 'a'},
        ],
    },
    'nev': {
        'name': '新能源整车',
        'keywords': ('新能源车', '新能源整车', '电动车', '造车新势力', '整车', '汽车整车'),
        'peers': [
            {'name': '比亚迪', 'code': '002594', 'market': 'a'},
            {'name': '赛力斯', 'code': '601127', 'market': 'a'},
            {'name': '理想汽车', 'code': 'HK2015', 'market': 'hk'},
            {'name': '小鹏汽车', 'code': 'HK9868', 'market': 'hk'},
        ],
    },
    'internet': {
        'name': '互联网平台',
        'keywords': ('互联网', '平台经济', '电商', '社交', '网游', '游戏'),
        'peers': [
            {'name': '腾讯控股', 'code': 'HK0700', 'market': 'hk'},
            {'name': '阿里巴巴', 'code': 'HK9988', 'market': 'hk'},
            {'name': '美团', 'code': 'HK3690', 'market': 'hk'},
            {'name': '小米集团', 'code': 'HK1810', 'market': 'hk'},
        ],
    },
}


# 个别代码 → 行业：补关键词识别的盲区（公司名里不含行业词，如"华域汽车"不含"零部件"）
CODE_INDUSTRY: Dict[str, str] = {
    '600660': 'auto_glass',   # 福耀玻璃
    '600741': 'auto_glass',   # 华域汽车
    '600699': 'auto_glass',   # 均胜电子
    'HK0868': 'auto_glass',   # 信义玻璃
    '600519': 'baijiu', '000858': 'baijiu', '000568': 'baijiu', '600809': 'baijiu',
    '601398': 'bank', '601939': 'bank', '600036': 'bank', '000001': 'bank',
}


def industry_of_code(code: Optional[str]) -> Optional[str]:
    """按代码直接查行业（先做代码归一化，兼容 `HK0868` / `0868` 两种写法）。"""
    c = str(code or '').strip().upper()
    if not c:
        return None
    if c in CODE_INDUSTRY:
        return CODE_INDUSTRY[c]
    if c.isdigit() and len(c) == 4:                # `0868` → `HK0868`
        return CODE_INDUSTRY.get('HK' + c)
    return None


# ==================== 行业识别 ====================


def classify_industry(*hints: Optional[str]) -> Optional[str]:
    """从公司名 / 行业字段 / 主营业务描述里识别行业键。识别不出返回 None（**不猜**）。"""
    blob = ' '.join(str(h) for h in hints if h).lower()
    if not blob:
        return None
    best: Optional[tuple] = None
    for key, spec in INDUSTRY_POOLS.items():
        hit = [k for k in spec['keywords'] if k.lower() in blob]
        if hit and (best is None or len(hit) > best[0]):
            best = (len(hit), key)
    return best[1] if best else None


def pool_for_industry(key: Optional[str]) -> List[Dict[str, Any]]:
    if not key or key not in INDUSTRY_POOLS:
        return []
    return [dict(p) for p in INDUSTRY_POOLS[key]['peers']]


def industry_name(key: Optional[str]) -> str:
    return (INDUSTRY_POOLS.get(key) or {}).get('name') or '未识别'


# ==================== 校验与自动替换 ====================


def _member_keys(peers: Sequence[Any]) -> set:
    """把一组同业归一化成可比较的键集合（名称与代码都收）。"""
    out = set()
    for p in peers or []:
        if isinstance(p, dict):
            for f in ('name', 'code'):
                v = str(p.get(f) or '').strip()
                if v:
                    out.add(v.lower())
        else:
            v = str(p).strip()
            if v:
                out.add(v.lower())
    return out


def is_fetchable_code(code: Optional[str]) -> bool:
    """该代码能否用行情快照取到：A 股 6 位 / 港股 `HK`+4~5 位。

    海外代码（`TSE:5201`、`EPA:SGO` 这类）本平台的行情链路取不到 → 返回 False，
    调用方**不得**把它们塞进需要实时快照的同业列表。
    """
    c = str(code or '').strip().upper()
    if not c or ':' in c:
        return False
    if c.startswith('HK'):
        return c[2:].isdigit() and 4 <= len(c[2:]) <= 5
    return c.isdigit() and len(c) == 6


def _code_tail(code: str) -> str:
    """`HK0868` → `0868`；`TSE:5201` → `5201`；`600741` → `600741`。"""
    c = str(code or '').strip().lower()
    for sep in (':',):
        if sep in c:
            c = c.split(sep)[-1]
    for pre in ('hk', 'sh', 'sz'):
        if c.startswith(pre):
            c = c[len(pre):]
    return c.lstrip('0') or '0'


def validate_peers(chosen: Sequence[Any], industry_key: Optional[str]) -> Dict[str, Any]:
    """判断上游给的同业是否属于该行业。

    返回 ``{'matched','mismatched','ratio','wrong','reason'}``。
    """
    pool = pool_for_industry(industry_key)
    if not pool:
        return {'matched': [], 'mismatched': list(chosen or []), 'ratio': 0.0,
                'wrong': False,
                'reason': f'行业未识别（{industry_name(industry_key)}），无法校验同业，保留原值'}
    pool_keys = _member_keys(pool)
    pool_tails = {_code_tail(p['code']) for p in pool}
    matched, mismatched = [], []
    for c in chosen or []:
        keys = _member_keys([c])
        tails = {_code_tail(v) for v in keys}
        if (keys & pool_keys) or (tails & pool_tails):
            matched.append(c)
        else:
            mismatched.append(c)
    ratio = round(len(matched) / max(1, len(chosen or [])), 2)
    wrong = bool(chosen) and not matched          # 一个都不匹配 → 选错
    reason = (f'同业校验：{len(matched)}/{len(chosen or [])} 命中「{industry_name(industry_key)}」池'
              if chosen else '上游未提供同业')
    if wrong:
        reason += f'，判定**选错行业**（正确池应为「{industry_name(industry_key)}」）'
    return {'matched': matched, 'mismatched': mismatched, 'ratio': ratio,
            'wrong': wrong, 'reason': reason}


def resolve_peers(*, code: str = '', industry_hint: str = '',
                  chosen: Optional[Sequence[Any]] = None,
                  name: str = '') -> Dict[str, Any]:
    """最终同业池决策：识别行业 → 校验 → 必要时**自动替换**。

    返回 ``{'industry','industry_cn','peers','switched','reason','validation',
             'source','codes'}``。
    """
    key = industry_of_code(code) or classify_industry(industry_hint, name, code)
    chosen = list(chosen or [])
    val = validate_peers(chosen, key)
    pool = pool_for_industry(key)
    switched = False
    if val['wrong'] and pool:
        final, source = pool, 'auto_pool'
        switched = True
        reason = (f"⚠️ 上游同业与识别行业不符（{val['reason']}）→ "
                  f"已自动切换为「{industry_name(key)}」同业池（{len(pool)} 家）")
    elif chosen:
        final, source = chosen, 'upstream'
        reason = val['reason']
    elif pool:
        final, source = pool, 'auto_pool'
        reason = (f"上游未提供同业 → 使用「{industry_name(key)}」同业池"
                  f"（{len(pool)} 家）")
    else:
        final, source = [], 'none'
        reason = ('行业未识别且上游未提供同业 → **不臆造同业**，'
                  '请在报告中标注「可比公司待补充」并说明行业归属依据')
    return {
        'industry': key, 'industry_cn': industry_name(key),
        'peers': final, 'switched': switched, 'reason': reason,
        'validation': val, 'source': source,
        'codes': [p.get('code') if isinstance(p, dict) else str(p) for p in final],
    }


def render_decision(decision: Dict[str, Any]) -> str:
    """一行式决策说明（可直接进报告）。"""
    peers = decision.get('peers') or []
    names = '、'.join((p.get('name') if isinstance(p, dict) else str(p)) for p in peers[:8])
    seg = f"**可比公司**（行业：{decision.get('industry_cn')}）：{names or '—'}"
    if decision.get('switched'):
        seg += f"\n> {decision['reason']}"
    elif not peers:
        seg += f"\n> {decision['reason']}"
    return seg
