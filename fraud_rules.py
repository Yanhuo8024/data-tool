#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fraud_rules.py - 信贷申请反欺诈规则引擎（确定性规则，不依赖模型）

**为什么单独成模块**：贷前环节要判的是两件不同的事——
「会不会还」是**信用风险**，用评分卡（`scorecard.py`）；
「是不是本人 / 是不是团伙」是**欺诈风险**，业界主要靠**规则 + 名单 + 关联图谱**，
而不是监督模型——因为欺诈样本极稀有（正例率常 <1%）、模式迭代快，
规则引擎的**可解释、可快速上线、可人工复核留痕**比模型精度更重要。

本模块实现三类最常用的团伙欺诈信号（全部确定性、可复现、可审计）：

  1. `device_cluster`    设备聚集：同一设备关联的**不同申请人**超阈值 —— 中介代办 / 团伙申请（HIGH）
  2. `velocity_burst`    短时高频：同一申请人在滑动窗口内申请次数超阈值 —— 撞库 / 批量提交（HIGH）
  3. `identity_overlap`  信息复用：手机号 / 单位 / 联系人被多申请人共用 —— 多头借贷 / 包装资料（MID）

裁定：任一 HIGH → `reject_review`（转人工反欺诈复核，**不得自动通过**）；
仅 MID → `manual_check`（转人工抽查）；无命中 → `pass`（可进入信用评分流程）。

与 `risk_review.py` 的分工：那个审「输出文本的合规性」，这个审「申请人的欺诈风险」。
输入为申请流水（`list[dict]` 或 pandas DataFrame），输出结构化命中，供人工与下游模型消费。

字段约定（缺字段的规则自动跳过该行，不报错）：
  `applicant_id` 申请人标识 ｜ `device_id` 设备指纹 ｜ `apply_time` 申请时间
  `phone` 手机号 ｜ `employer` 单位 ｜ `contact_phone` 联系人手机号（可为列表）
"""
import datetime as _dt
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 阈值集中在一处：便于审计、便于按业务调整，不散落在各规则里
DEFAULT_THRESHOLDS: Dict[str, Any] = {
    'device_max_applicants': 3,     # 同一设备关联申请人数的上限
    'velocity_window_hours': 24,    # 短时高频的滑动窗口（小时）
    'velocity_max_applies': 3,      # 窗口内申请次数上限
    'overlap_min_shared': 2,        # 同一信息项被几个申请人共用即视为异常
}

_TIME_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d',
                 '%Y/%m/%d %H:%M:%S', '%Y/%m/%d')


def _as_rows(data: Any) -> List[Dict[str, Any]]:
    """把 DataFrame 或 list[dict] 统一成 list[dict]；无法识别的输入返回空列表。"""
    if data is None:
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    to_dict = getattr(data, 'to_dict', None)
    if callable(to_dict):
        try:
            return list(to_dict('records'))
        except Exception as e:                     # 非法 DataFrame 不应让整批申请失败
            logger.debug("fraud_rules: DataFrame 转 records 失败: %s", e)
            return []
    return []


def _parse_ts(v: Any) -> Optional[float]:
    """申请时间 → Unix 秒。解析失败返回 None（该行不参与时间类规则，而非拖垮整批）。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, _dt.datetime):
        return v.timestamp()
    s = str(v).strip()
    if not s:
        return None
    for fmt in _TIME_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).timestamp()
        except ValueError as e:
            logger.debug("fraud_rules: 时间 %r 不匹配格式 %s (%s)", v, fmt, e)
            continue
    try:
        return _dt.datetime.fromisoformat(s).timestamp()
    except ValueError as e:
        logger.debug("fraud_rules: 无法解析申请时间 %r: %s", v, e)
        return None


def _vals(v: Any) -> List[str]:
    """把字段值规范成非空字符串列表（兼容单值与列表两种写法）。"""
    items = v if isinstance(v, (list, tuple, set)) else [v]
    out = []
    for x in items:
        s = str(x if x is not None else '').strip()
        if s:
            out.append(s)
    return out


def check_device_cluster(rows: List[Dict[str, Any]], th: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """设备聚集：同一设备关联的**不同申请人**数超阈值 → 中介代办 / 团伙申请。"""
    by_dev: Dict[str, set] = {}
    for r in rows:
        dev = str(r.get('device_id') or '').strip()
        app = str(r.get('applicant_id') or '').strip()
        if dev and app:
            by_dev.setdefault(dev, set()).add(app)
    hits = sorted(((d, sorted(a)) for d, a in by_dev.items()
                   if len(a) > th['device_max_applicants']),
                  key=lambda x: -len(x[1]))
    if not hits:
        return None
    return {
        'rule': 'device_cluster', 'level': 'HIGH', 'label': '设备聚集（疑似团伙 / 中介代办）',
        'note': f"{len(hits)} 个设备关联申请人超过 {th['device_max_applicants']} 人，"
                f"最多 1 台设备关联 {len(hits[0][1])} 人",
        'evidence': [{'device': d, 'applicants': a} for d, a in hits[:5]],
    }


def check_velocity_burst(rows: List[Dict[str, Any]], th: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """短时高频：同一申请人在滑动窗口内申请次数超阈值 → 撞库 / 批量提交。"""
    win = float(th['velocity_window_hours']) * 3600.0
    maxn = th['velocity_max_applies']
    by_app: Dict[str, List[float]] = {}
    unparsed = 0
    for r in rows:
        app = str(r.get('applicant_id') or '').strip()
        if not app:
            continue
        ts = _parse_ts(r.get('apply_time'))
        if ts is None:
            unparsed += 1
            continue
        by_app.setdefault(app, []).append(ts)
    hits = []
    for app, tss in by_app.items():
        tss.sort()
        best, j = 1, 0
        for i in range(len(tss)):                  # 双指针求窗口内最大申请数
            while tss[i] - tss[j] > win:
                j += 1
            best = max(best, i - j + 1)
        if best > maxn:
            hits.append((app, best))
    if not hits:
        return None
    hits.sort(key=lambda x: -x[1])
    note = (f"{len(hits)} 个申请人在 {th['velocity_window_hours']}h 内申请超过 {maxn} 次，"
            f"最多 {hits[0][1]} 次")
    if unparsed:                                   # 诚实标注未参与计算的行数
        note += f"（{unparsed} 条申请时间无法解析，未参与本规则）"
    return {
        'rule': 'velocity_burst', 'level': 'HIGH', 'label': '短时高频申请（疑似撞库 / 批量提交）',
        'note': note,
        'evidence': [{'applicant': a, 'applies_in_window': n} for a, n in hits[:5]],
    }


def check_identity_overlap(rows: List[Dict[str, Any]], th: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """信息复用：手机号 / 单位 / 联系人被多个申请人共用 → 多头借贷 / 资料包装。"""
    fields = ('phone', 'employer', 'contact_phone')
    by_val: Dict[str, Dict[str, set]] = {f: {} for f in fields}
    for r in rows:
        app = str(r.get('applicant_id') or '').strip()
        if not app:
            continue
        for f in fields:
            for s in _vals(r.get(f)):
                by_val[f].setdefault(s, set()).add(app)
    hits = [{'field': f, 'value': v, 'applicants': sorted(a)}
            for f in fields for v, a in by_val[f].items()
            if len(a) > th['overlap_min_shared']]
    if not hits:
        return None
    hits.sort(key=lambda x: -len(x['applicants']))
    return {
        'rule': 'identity_overlap', 'level': 'MID', 'label': '信息复用（多头借贷 / 包装资料）',
        'note': f"{len(hits)} 项信息被超过 {th['overlap_min_shared']} 个申请人共用，"
                f"最多 {len(hits[0]['applicants'])} 人共用同一项",
        'evidence': hits[:5],
    }


def review(rows: Any, thresholds: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """对一批申请流水跑全部反欺诈规则，返回裁定与命中明细。

    单条规则内部异常不影响其余规则（各自 try 住并记日志）——反欺诈宁可多报也不可整批漏审。
    """
    th = dict(DEFAULT_THRESHOLDS)
    if isinstance(thresholds, dict):
        th.update({k: v for k, v in thresholds.items() if k in DEFAULT_THRESHOLDS})
    rs = _as_rows(rows)
    hits = []
    for fn in (check_device_cluster, check_velocity_burst, check_identity_overlap):
        try:
            h = fn(rs, th)
        except Exception as e:
            logger.warning("fraud_rules: 规则 %s 执行失败: %s", fn.__name__, e)
            h = None
        if h:
            hits.append(h)
    high = [h for h in hits if h.get('level') == 'HIGH']
    if high:
        verdict, action = 'reject_review', '转人工反欺诈复核（HIGH 信号），复核前不得自动通过'
    elif hits:
        verdict, action = 'manual_check', '转人工抽查（MID 信号），可并行进入信用评分'
    else:
        verdict, action = 'pass', '未命中反欺诈规则，可进入信用风险评分流程'
    return {
        'ok': True,
        'n_rows': len(rs),
        'verdict': verdict,
        'handoff': bool(hits),
        'n_high': len(high),
        'n_mid': len(hits) - len(high),
        'hits': hits,
        'action': action,
        'thresholds': th,
    }


def render(result: Dict[str, Any]) -> str:
    """把 `review()` 的结果渲染成可读文本（供接口/AI 工具直接返回）。"""
    if not isinstance(result, dict) or not result.get('ok'):
        return "反欺诈扫描未取得有效结果。"
    lines = [f"【贷前反欺诈扫描】样本 {result.get('n_rows', 0)} 条 · 裁定：{result.get('verdict')}"]
    for h in result.get('hits') or []:
        lines.append(f"  [{'⚠ 高危' if h['level'] == 'HIGH' else '! 关注'}] {h['label']} —— {h['note']}")
    lines.append(f"  处置：{result.get('action', '')}")
    if not result.get('hits'):
        lines.append("  未命中规则，不代表无欺诈风险；欺诈模式迭代快，建议配合名单与关联图谱。")
    return "\n".join(lines)


def rule_catalog() -> List[Dict[str, str]]:
    """规则清单（供前端展示 / 接口自描述）。"""
    return [
        {'rule': 'device_cluster', 'level': 'HIGH', 'label': '设备聚集（疑似团伙 / 中介代办）',
         'threshold': 'device_max_applicants'},
        {'rule': 'velocity_burst', 'level': 'HIGH', 'label': '短时高频申请（疑似撞库 / 批量提交）',
         'threshold': 'velocity_window_hours + velocity_max_applies'},
        {'rule': 'identity_overlap', 'level': 'MID', 'label': '信息复用（多头借贷 / 包装资料）',
         'threshold': 'overlap_min_shared'},
    ]
