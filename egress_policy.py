#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""egress_policy.py — 数据出境策略中心（默认值显式 / 可查 / 可关）

本模块集中管理"用户数据离开本机"的两条**可关闭**通道，供三处共用：
工具注册（ai_agent 的 ToolRegistry）、运行时（web_search / file_rag）、设置面板（路由 + 前端）。

    ALLOW_WEB_SEARCH           联网搜索：上行由问题清洗出的搜索关键词到百度/必应/DuckDuckGo
    ALLOW_FILE_SAMPLE_EGRESS   文件样本：上传数据文件的样本行进入云端模型 Prompt

取值优先级：**环境变量（部署方强制） > data/egress_policy.json（面板持久化） > 默认开启**。
环境变量一旦显式设置即为终值，面板改不动它，避免"运维关了、界面又被打开"。

设计原则承接 ZCode 事件教训：联网与内容出境这类默认开启的行为，必须
① 默认值可查；② 有开关；③ 开关位置可见；④ 当前生效值可读。
清单见 design/DATA_EGRESS.md，结构由 tools/arch_guard.py 的 R8 守护（新增出站未登记即报警）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 策略文件：测试可用 EGRESS_POLICY_FILE 重定向（conftest 亦会隔离）
_DEFAULT_STORE = Path(__file__).resolve().parent / 'data' / 'egress_policy.json'

# 功能定义：key → (环境变量, 面板名称, 上行内容, 关闭后行为)
FEATURES = {
    'web_search': {
        'env': 'ALLOW_WEB_SEARCH',
        'label': '联网搜索',
        'upstream': '由提问清洗出的搜索关键词 → 百度 / 必应 / DuckDuckGo',
        'when_off': 'web_search 工具不再注册，提问不会被送往搜索引擎',
    },
    'file_sample_egress': {
        'env': 'ALLOW_FILE_SAMPLE_EGRESS',
        'label': '文件样本出境',
        'upstream': '上传数据文件的前 6000 字符样本行 / 文档正文 → 云端模型 Prompt',
        'when_off': '仅上传列名与统计量（describe）、文档只出元信息，原文不再随提问发出',
    },
    'image_egress': {
        'env': 'ALLOW_IMAGE_EGRESS',
        'label': '图片出境（识图）',
        'upstream': '用户在对话中上传/粘贴的图片（base64 原文，最多 4 张）→ 云端多模态模型',
        'when_off': '识图请求直接返回说明，图片不会送往云端模型',
    },
}
DEFAULTS = {k: True for k in FEATURES}
_TRUTHY = {'1', 'true', 'yes', 'on', 'y', 't'}
_FALSY = {'0', 'false', 'no', 'off', 'n', 'f', ''}

_file_cache: dict = {}  # {'sig': (mtime, size), 'data': {...}}


def store_path() -> Path:
    """策略文件路径（每次读环境变量，便于测试与多实例）"""
    return Path(os.environ.get('EGRESS_POLICY_FILE') or _DEFAULT_STORE)


def _parse_bool(raw):
    """宽松解析布尔；无法识别返回 None（调用方回退下一优先级）"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in _TRUTHY:
        return True
    if s in _FALSY:
        return False
    return None


def _load_file() -> dict:
    """读持久化值；文件不存在/损坏 → 空 dict（不抛异常，避免拖垮工具注册）"""
    p = store_path()
    try:
        st = p.stat()
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    if _file_cache.get('sig') == sig:
        return _file_cache.get('data') or {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        data = data if isinstance(data, dict) else {}
    except (ValueError, OSError) as e:
        logger.warning('数据出境策略文件无法解析，按默认值处理: %s', e)
        data = {}
    _file_cache['sig'] = sig
    _file_cache['data'] = data
    return data


def _env_value(key: str):
    return _parse_bool(os.environ.get(FEATURES[key]['env']))


def get_flag(key: str) -> bool:
    """当前生效值：环境变量 > 持久化 > 默认开启"""
    if key not in FEATURES:
        raise KeyError(f'未知出境策略项: {key}')
    env = _env_value(key)
    if env is not None:
        return env
    file_val = _parse_bool(_load_file().get(key))
    return DEFAULTS[key] if file_val is None else file_val


def flag_source(key: str) -> str:
    """生效值来自哪里：env（部署方强制）/ file（面板设置）/ default"""
    if _env_value(key) is not None:
        return 'env'
    if _parse_bool(_load_file().get(key)) is not None:
        return 'file'
    return 'default'


def set_flag(key: str, value: bool) -> bool:
    """写入持久化值并返回是否成功。环境变量已显式设置时拒绝改写（返回 False）。"""
    if key not in FEATURES:
        raise KeyError(f'未知出境策略项: {key}')
    if _env_value(key) is not None:
        logger.warning('%s 已由环境变量 %s 强制，面板设置被忽略', key, FEATURES[key]['env'])
        return False
    p = store_path()
    data = dict(_load_file())
    data[key] = bool(value)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + '.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, p)
        _file_cache.clear()
        return True
    except OSError as e:
        logger.error('写入数据出境策略失败: %s', e)
        return False


# ------------------------------ 便捷取用 API ------------------------------
def web_search_enabled() -> bool:
    """联网搜索是否允许（False → 工具不注册、调用直接返回关闭提示）"""
    return get_flag('web_search')


def file_sample_egress_enabled() -> bool:
    """文件样本行是否允许进入云端 Prompt（False → file_rag 只回列名与统计量）"""
    return get_flag('file_sample_egress')


def image_egress_enabled() -> bool:
    """用户上传的图片是否允许送往云端多模态模型（False → 识图分支直接拒绝，不外呼）"""
    return get_flag('image_egress')


def vision_blocked_reply() -> Optional[dict]:
    """识图分支的统一拒绝响应；允许出境时返回 None。

    与 web_search 关闭同口径：**先拦在发图之前**，不是发完了再提示。
    返回 None 表示放行，调用方照常走视觉模型分支。
    """
    try:
        if image_egress_enabled():
            return None
    except (KeyError, OSError) as e:                     # 策略不可读 → 保持默认（开启）
        logger.warning('读取出境策略失败，识图按默认（可用）处理: %s', e)
        return None
    f = FEATURES['image_egress']
    return {
        'reply': (f'⚠️ {f["label"]}已在「数据出境」设置中关闭（{f["env"]}=0），'
                  '本次图片未发送到云端模型。如需识图，请到 AI 助手 ⚙️ 设置 → 数据出境中开启；'
                  '也可以把图中内容改成文字描述后提问。'),
        'source': 'vision',
        'llm_synthesized': False,
        'intent': {'action': 'chat', 'params': {}, 'confidence': 0.9, 'raw_text': ''},
    }


def vision_blocked_for(message: str) -> Optional[dict]:
    """识图出境的**带原始问题**版本（ai_assistant 的识图闸门唯一入口）。

    同步路径直接 return 该 dict；流式路径把 reply 包进 done 事件——两处共用同一判定，
    防止"同步拦住了、流式还在发图"。策略模块不可读时按默认放行并留痕，不误伤正常识图。
    实现在此处而非 ai_assistant（该文件受架构守卫 R1 行数棘轮约束）。
    """
    try:
        blocked = vision_blocked_reply()
    except Exception as e:                                # 策略模块异常不得阻断识图
        logger.warning('识图出境闸门不可用，按放行处理: %s', e)
        return None
    if not blocked:
        return None
    blocked['intent']['raw_text'] = message
    return blocked


def disabled_reason(key: str) -> str:
    """给用户/模型看的关闭说明"""
    f = FEATURES[key]
    return (f'{f["label"]}已在设置中关闭（{f["env"]}）。原始说明：'
            f'开启时会{ "上行：" + f["upstream"] }。可在 AI 助手 ⚙️ 设置 → 数据出境中开启。')


# --------------------------- Agent 工具闸门 ---------------------------
# 工具名 → 所属出境策略项。被禁用的工具：不进 prompt、不可执行（分发时给明确原因）。
GATED_TOOLS = {'web_search': 'web_search'}


# --------------------------- 工具风险语义（P0，对标 dsh 权限插件） ---------------------------
# 为什么需要：`GATED_TOOLS` 只约束「数据出境」这一个**方向**（东西往外发），
# 对「工具本身危不危险」是盲区 —— 写盘工具与只读工具此前享受完全相同的信任级别。
# 本表补上**第二个方向**：副作用。
#
# 分级语义（粗粒度三档，刻意不做细粒度权限系统 —— 收益/成本不成立）：
#   'readonly'  只读取数，无持久副作用                      → 默认放行
#   'write'     会写盘 / 改库 / 训练产出模型                 → 默认放行，可被干预
#   'external'  会发出站请求（联网 / 直连外部数据库）        → 默认放行，可被干预
#
# ⚠️ **默认值保持"放行"是刻意的**：本项目是本地自用工具，默认拦截会打断现有工作流。
#    本层提供的是**能力**（可查、可拦、可登记），不是默认行为变更。
# `ALLOW_TOOL_SIDE_EFFECTS=0` 时，write / external 两类统一拦截。
# 未登记的工具一律按 'readonly' 处理（放行），保证新增工具零回归 —— 新增时应补登记。
TOOL_RISK = {
    # --- 写盘：改文件系统 / 落模型产物 ---
    'clean_data': 'write',        # 清洗结果落盘
    'feature_eng': 'write',       # 特征工程产物落盘
    'ml_train': 'write',          # 训练产出模型文件
    'generate_chart': 'write',    # 图表文件落盘
    'run_analysis_api': 'write',  # 可触达后端**任意**分析端点，含写盘类（风险面最宽的一个）
    # --- 出站：联网 / 外部数据库 ---
    'web_search': 'external',
    'db_query': 'external',       # 直连外部数据库（只读 SQL，但连接即出站）
}

_SIDE_EFFECT_KINDS = ('write', 'external')


def tool_risk(name: str) -> str:
    """该工具的副作用等级：readonly / write / external（未登记 = readonly）"""
    return TOOL_RISK.get(name, 'readonly')


def tool_side_effect_allowed(name: str) -> bool:
    """工具副作用是否被放行。仅对 write/external 生效；readonly 永远放行。

    与 `tool_allowed`（数据出境方向）**正交**：一个是"东西能不能出去"，
    一个是"工具能不能动副作用"。两者都通过才允许执行。
    """
    if tool_risk(name) not in _SIDE_EFFECT_KINDS:
        return True
    return side_effects_enabled()


def side_effects_enabled() -> bool:
    """副作用开关的**全局**状态（与具体工具名无关）。

    单独抽出来的原因：`tool_side_effect_allowed()` 对未登记工具恒为 True，
    所以**不能用任意工具名去探测开关状态** —— 用 `'__probe__'` 之类的占位名会
    永远读成"放行"，快照/面板就会说谎（本函数正是为修此缺陷而设）。
    """
    try:
        return _parse_bool(os.environ.get('ALLOW_TOOL_SIDE_EFFECTS', '1')) is not False
    except (KeyError, OSError) as e:
        logger.warning('读取工具副作用策略失败，按默认（放行）处理: %s', e)
        return True


def tool_side_effect_message(name: str) -> str:
    """副作用被拦截时给模型的执行反馈"""
    kind = tool_risk(name)
    _desc = '写盘/改库' if kind == 'write' else '发出站请求'
    return (f'工具 {name} 属「{kind}」类（{_desc}），当前被 ALLOW_TOOL_SIDE_EFFECTS=0 拦截，'
            f'本次未产生任何副作用。若仅为只读分析，请改用 describe / head / columns / '
            f'correlation / trend / hypothesis_test 等只读取数工具；'
            f'如确需该操作，请让用户调整环境变量或到 AI 助手 ⚙️ 设置中开启。')


def tool_risk_snapshot() -> dict:
    """工具风险清单（面板 / 诊断接口用）：按等级分组，供前端渲染"哪些工具会动副作用"。"""
    groups = {k: [] for k in ('readonly', 'write', 'external')}
    for name in sorted(TOOL_RISK):
        groups[tool_risk(name)].append(name)
    return {
        'ok': True,
        'levels': groups,
        'side_effects_allowed': side_effects_enabled(),
        'env': 'ALLOW_TOOL_SIDE_EFFECTS',
        'policy': '未登记工具按 readonly 处理；登记项见 TOOL_RISK',
    }


def tool_allowed(name: str) -> bool:
    """该工具当前是否允许使用（未受管制的工具一律放行）"""
    key = GATED_TOOLS.get(name)
    if key is None:
        return True
    try:
        return get_flag(key)
    except (KeyError, OSError) as e:  # 策略不可读时保持默认（开启），并留下痕迹
        logger.warning('读取出境策略失败，%s 按默认（可用）处理: %s', name, e)
        return True


def tool_disabled_message(name: str) -> str:
    """被禁用工具的执行反馈（模型读到后应改用其他工具或本地数据）"""
    key = GATED_TOOLS.get(name, name)
    f = FEATURES.get(key)
    if not f:
        return f'工具 {name} 当前不可用。'
    return (f'工具 {name} 已被「数据出境」设置禁用（{f["env"]}=0），本次未发出任何外呼。'
            f'请改用本地数据/工具作答；如需联网，请让用户到 AI 助手 ⚙️ 设置 → 数据出境中开启。')


def snapshot() -> dict:
    """面板 / 接口用的当前状态（含默认值与来源，回答"默认开着还是关着"）"""
    items = []
    for key, f in FEATURES.items():
        items.append({
            'key': key,
            'env': f['env'],
            'label': f['label'],
            'enabled': get_flag(key),
            'default': DEFAULTS[key],
            'source': flag_source(key),
            'locked': flag_source(key) == 'env',
            'upstream': f['upstream'],
            'when_off': f['when_off'],
        })
    return {'ok': True, 'features': items,
            'store': str(store_path()),
            'precedence': '环境变量 > 设置面板（持久化） > 默认开启',
            'doc': 'design/DATA_EGRESS.md'}


def reset_cache():
    """清空文件缓存（测试与外部改文件后调用）"""
    _file_cache.clear()
