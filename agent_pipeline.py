#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_pipeline.py - 多 Agent 协作管线（CrewAI 式分工，金融研究特化）

市场分析师（取数与基本面）∥ 量化建模师（量化验证与测算）∥ 风控合规官（口径/风险/合规审查）
各自独立 ReAct 循环；命中阶段**并行执行**（2026-09-18 用户指定：用不同模型同时跑，缩短总耗时），
全部完成后由终稿合成（首席分析师，无工具单次调用）汇总成稿。
并行 trade-off：阶段间不再逐级传递产出，交叉验证由终稿合成统一完成。

**阶段按需命中**（2026-09-18 晚，用户指定）：不再无脑三阶段全跑，由
`select_pipeline_stages()` 按任务实际需要只跑命中的阶段（纯规则判定，零额外 LLM 调用）——
纯行业研究只跑市场分析师、估值/统计测算只跑量化建模师、风险合规审查只跑风控合规官；
"全面分析/完整分析/尽调"等显式全域词 → 三阶段全跑；全不命中 → 保底市场分析师。
`PIPELINE_STAGE_ROUTING=0` 可总回退为三阶段全跑。

触发条件（成本护栏）：仅当消息显式包含高价值关键词（研究报告/全面分析/尽调/深度研究/完整分析）
时启用——命中阶段各自独立 ReAct 循环，会成倍消耗 LLM 额度，普通分析仍走单体 Agent。

与 ui 的对接：进度事件（status/progress/ping/done）与 ai_assistant._stream_agent_analysis
完全同构，前端思考过程面板无需任何改动即可展示三阶段过程（progress 事件额外带 stage 字段）。
"""
import logging
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 成本护栏：显式高价值关键词才启用管线
PIPELINE_TRIGGER_KW = ('研究报告', '全面分析', '尽调', '深度研究', '完整分析')


def should_use_pipeline(message: str) -> bool:
    """是否启用多 Agent 协作管线（显式高价值请求才启用，控制额度消耗）"""
    if not message:
        return False
    return any(k in message for k in PIPELINE_TRIGGER_KW)


def _stage_llm_config(base, stage_name, pm_map):
    """按阶段名返回该阶段使用的 LLM 配置。

    - pm_map 为空/N 命中该 stage：返回 base 的浅拷贝，逐字节等价（老路径零回归）。
    - 命中且换 provider：仅覆盖 provider/model，并清空 apiUrl/apiKey，
      交由 llm_client._resolve_* 用官方端点/服务端密钥补全（不新增明文暴露面）。
    - 命中且同 provider：仅换 model。
    - 入参畸形（pm_map 非 dict、某阶段值非 dict、缺 provider/model）一律回落 base——
      本函数是 run_pipeline_stream 的公开入参路径，必须对脏输入零崩溃。
    """
    cfg = dict(base or {})
    pm = pm_map.get(stage_name) if isinstance(pm_map, dict) else None
    if isinstance(pm, dict) and pm.get('provider') and pm.get('model'):
        prov = pm['provider']
        if prov != (base or {}).get('provider'):
            cfg.update(provider=prov, model=pm['model'], apiUrl='', apiKey='')
        else:
            cfg['model'] = pm['model']
    return cfg


# 多Agent协作可独立指定模型的阶段名（与 STAGE_DEFS + 终稿合成对齐）
PIPELINE_STAGE_NAMES: tuple = ('市场分析师', '量化建模师', '风控合规官', '终稿合成')


# 阶段模型钉扎（用户指定，2026-09-18）：
#   市场分析师 → glm-5.2（市场面研究，综合能力强）
#   量化建模师 → sensenova-6.8-flash-lite（原 deepseek-v4-pro，因持续 429 于 2026-09-18 晚替换）
#   风控合规官 → deepseek-v4-flash（轻量审查，速度快）
# 三个不同模型 + 三阶段并行（用户 2026-09-18 指定"用三个不同的模型来跑，增加速度"）。
# 只要 sensenova 在可用列表内就强制钉扎（优先于轮换分配）；不可用时维持轮换结果。
# 模型均为 sensenova 实测可调用（2026-09-17 探测 200），不硬塞给其他 provider。
_STAGE_MODEL_PREFS: Dict[str, Dict[str, str]] = {
    '市场分析师': {'provider': 'sensenova', 'model': 'glm-5.2'},
    # 2026-09-18 晚：deepseek-v4-pro 实测**持续限流**——两次真实生产运行（15:47 / 20:06）
    # 均返回 429 `inference exceeds tpm/rpm limit`，量化阶段只产出 114 字符却被记 ok=true。
    # 同账号实调确认 sensenova-6.8-flash-lite 可用（200，有正常产出），故替换之；
    # 仍是三个**互不相同**的模型，满足"三阶段各用不同模型"的诉求。
    '量化建模师': {'provider': 'sensenova', 'model': 'sensenova-6.8-flash-lite'},
    '风控合规官': {'provider': 'sensenova', 'model': 'deepseek-v4-flash'},
}


def resolve_auto_pipeline_models(base_cfg: dict = None) -> Optional[Dict[str, Dict[str, str]]]:
    """自动为多Agent各阶段挑选可用大模型（用户无需手动逐阶段选择）。

    策略：
    1. 探测当前**确有凭据**的云端 provider（`llm_client.list_available_providers`，零网络请求）；
    2. 若全局 `base_cfg` 指定的 provider 在可用列表内，将其提到首位（尊重用户的全局选择）；
    3. 在阶段间**轮换分配**——各阶段尽量落到不同 provider，摊薄单一 provider 的免费额度
       rpm 限制（这是 429 的主要成因，也是"多模型协作"的实际收益点）；
    4. 仅 1 个可用 provider 时四阶段统一使用它；0 个可用时返回 None（调用方沿用全局 llm_config）。
    5. 阶段钉扎（`_STAGE_MODEL_PREFS`）：市场分析师/量化建模师/风控合规官只要 sensenova
       可用就分别固定 glm-5.2 / sensenova-6.8-flash-lite / deepseek-v4-flash（用户指定三模型并行），
       优先于轮换；sensenova 不可用时维持轮换结果。

    Args:
        base_cfg: 全局 LLM 配置（可含 provider/model），可为 None。

    Returns:
        {阶段名: {'provider': str, 'model': str}}；无可选 provider 时 None。
    """
    try:
        from llm_client import list_available_providers
        provs = list_available_providers()
    except Exception as e:  # 凭据库异常不应阻断管线——直接回退全局模型
        logger.warning("自动选择大模型失败（探测阶段）: %s", e)
        return None
    if not provs:
        return None

    base_prov = str((base_cfg or {}).get('provider') or '').strip().lower()
    base_model = str((base_cfg or {}).get('model') or '').strip()

    ordered: List[Dict[str, str]] = []
    if base_prov:
        for p in provs:
            if p['provider'] == base_prov:
                # 全局已选定该 provider 时沿用其 model（用户可能指定了特定版本）
                ordered.append({'provider': p['provider'],
                                'model': base_model or p['model']})
                break
    for p in provs:
        if not any(o['provider'] == p['provider'] for o in ordered):
            ordered.append({'provider': p['provider'], 'model': p['model']})

    if not ordered:
        return None

    out: Dict[str, Dict[str, str]] = {}
    _final_stage = PIPELINE_STAGE_NAMES[-1]
    for i, name in enumerate(PIPELINE_STAGE_NAMES):
        # 终稿合成 = 最终交付物，固定用首选 provider 的模型（ordered[0]），不参与轮换。
        # 起因（2026-09-18 回归）：阶段数 4 与 provider 数 2 不整除时，轮换把终稿落到
        # 次选 provider 的弱模型（实测 zhipu/glm-4-flash），而旧版单 provider 环境下终稿
        # 一直是 sensenova/glm-5.2。中间三阶段本就是"多模型协作"的轮换价值所在，
        # 终稿没有摊薄额度的必要，应优先保证质量。
        pick = ordered[0] if name == _final_stage else ordered[i % len(ordered)]
        out[name] = {'provider': pick['provider'], 'model': pick['model']}
    # 阶段钉扎（用户指定）：sensenova 可用时市场/量化/风控三阶段分别固定三个不同模型
    # （三阶段并行执行），优先于轮换分配；终稿合成不钉，沿用上面的首选模型。
    for name, pref in _STAGE_MODEL_PREFS.items():
        if any(p['provider'] == pref['provider'] for p in ordered):
            out[name] = dict(pref)
    return out


# ==================== 三阶段提速配置（2026-09-18）====================
# 目标：三个中间阶段的"分析推理"全面加速。实测瓶颈有四处（见 .trae/documents/
# pipeline_stage_speedup_20260918.md）：
#   ① 每阶段白跑 1~2 次规划 LLM 调用（管线已用 focus 给定角色，规划纯属重复）；
#   ② 每阶段下发全部 32 个工具的 schema（tools_prompt 5685 字符，每轮迭代重发）；
#   ③ 每阶段额外注入技能正文/知识点库/DID·面板·金融 CoT 框架（每轮重发的大宗 token）；
#   ④ 同轮多个工具调用串行执行（联网工具 2~10s/个）。
# PIPELINE_STAGE_FAST=0 可整体回退到改造前行为（逐项也可用各自环境变量回退）。
_STAGE_FAST = os.environ.get('PIPELINE_STAGE_FAST', '1') not in ('0', '', 'false', 'False')

# 阶段工具白名单：按职责裁剪（不再 32 个全下发）。
# 原则：只给该阶段职责真正需要的工具 + 极少量通用兜底（数据探查/文件列举）。
_STAGE_TOOL_WHITELIST: Dict[str, List[str]] = {
    '市场分析师': [
        'web_search',            # 行业规模/格局/政策/新闻
        'company_analysis',      # 公司基本面
        'finance_data',          # 行情与财务数据
        'financial_ratios',      # 财务比率
        'stock_valuation',       # 估值
        'financial_statements',  # 三大报表
        'safe_calc',             # 简单计算
        'list_files',            # 确认可用数据文件
    ],
    '量化建模师': [
        'stock_valuation',
        'financial_ratios',
        'financial_statements',
        'finance_data',
        'company_analysis',
        'describe', 'head', 'columns', 'overview',   # 数据探查
        'correlation', 'trend', 'percent_change',    # 量化测算
        'adf_test', 'hypothesis_test',               # 统计检验
        'safe_calc', 'generate_chart',
        'list_files',
    ],
    '风控合规官': [
        'web_search',            # 政策/合规口径核验
        'financial_ratios',      # 口径一致性核验
        'company_analysis',
        'safe_calc',
        'list_files',
    ],
}


# ==================== 阶段按需命中（2026-09-18 晚）====================
# 用户诉求：三阶段不再无脑全跑——"命中了这三个阶段中的哪一个，就用哪个分析"。
# 每多命中一个阶段就多一条独立 ReAct 循环（额度 + 耗时），按需命中是直接的省额提速。
# 判定采用**纯规则**（零额外 LLM 调用、毫秒级、可复现、可单测）：
#   ① 显式全域词（全面分析/完整分析/尽调…）→ 用户已明说"要全"，三阶段全跑；
#   ② 职责关键词命中 → 纳入对应阶段；
#   ③ 挂了数据文件 → 补「量化建模师」（有数据才有量化测算的对象）；
#   ④ 全不命中 → 保底「市场分析师」（能进管线的都是研究型请求，市场面是基座）。
# PIPELINE_STAGE_ROUTING=0 总回退为三阶段全跑（改造前行为）。
_STAGE_ROUTING = os.environ.get('PIPELINE_STAGE_ROUTING', '1') not in ('0', '', 'false', 'False')

# 显式"全域"要求：命中即三阶段全跑（用户已明确要求全面，不算浪费额度）。
# 用"全域强度词 + 作业对象词"组合判定，而非固定短语——中文里"完整**的**研究报告"
# 之类插词极常见，写死 '完整研究' 会漏判（实测踩过）。
_FULL_SCOPE_STRONG: tuple = ('全面', '完整', '全方位', '尽调')
_FULL_SCOPE_TARGET: tuple = ('分析', '研究', '报告', '评估', '方案', '审查', '尽调')

# 阶段职责关键词（取名词粒度，避免误伤；英文一律小写——匹配前消息已 lower）
_STAGE_HIT_KW: Dict[str, tuple] = {
    '市场分析师': (
        '行业', '市场', '竞争', '格局', '份额', '政策', '新闻', '事件', '产业链',
        '赛道', '规模', '增速', '景气', '需求', '供给', '上下游',
        '公司', '基本面', '财报', '业绩', '营收', '利润', '市值', '股价', '标的',
        '上市', '估值', '券商', '机构', '龙头',
    ),
    '量化建模师': (
        '量化', '建模', '模型', '测算', '预测', '回测', '因子', '回归',
        '相关性', '相关分析', '相关系数', '统计', '检验', '显著性', '敏感',
        '波动率', '收益率', '夏普', '最大回撤', '年化', '复权', '面板',
        '指标', '计算', '对比', '趋势', '增长率', 'cagr', 'dcf', 'roe',
        '均值', '中位数', '方差', '标准差', '分位数', '分布', '聚类', '分箱', '描述统计',
    ),
    '风控合规官': (
        '风险', '合规', '审查', '审核', '免责', '监管', '法规', '法律', '口径',
        '风控', '违约', '信用风险', '压力测试', '红线', '内控', '信息披露',
    ),
}


def select_pipeline_stages(message: str, current_file: str = None) -> List[str]:
    """按用户任务的实际需要挑选要执行的协作阶段。

    返回阶段名列表，顺序恒等于 STAGE_DEFS（稳定可复现）。纯规则判定：无 LLM、无 IO。
    PIPELINE_STAGE_ROUTING=0 → 三阶段全跑（回退改造前行为）。
    """
    names = [s['name'] for s in STAGE_DEFS]
    if not _STAGE_ROUTING:
        return names
    txt = str(message or '').lower()
    # ① 显式全域要求 → 全跑
    if (any(k in txt for k in _FULL_SCOPE_STRONG)
            and any(k in txt for k in _FULL_SCOPE_TARGET)):
        return names
    # ② 职责关键词命中
    hit = [n for n in names if any(k in txt for k in _STAGE_HIT_KW.get(n, ()))]
    # ③ 有数据文件 → 量化阶段有分析对象
    if current_file and '量化建模师' in names and '量化建模师' not in hit:
        hit.append('量化建模师')
    # ④ 空集保底
    if not hit:
        hit = ['市场分析师']
    return [n for n in names if n in hit]


# 三阶段分工定义（角色 → 职责 → 迭代预算）
STAGE_DEFS: List[Dict[str, Any]] = [
    {
        'name': '市场分析师',
        'focus': (
            '你是市场分析师。职责：围绕用户任务完成行业/公司/市场面研究——'
            '用 web_search 获取行业规模、竞争格局、政策与新闻；有具体股票标的时用金融工具'
            '（company_analysis / financial_ratios / finance_data）取真实财务与估值数据。'
            '产出【事实性研究笔记】：只陈述有工具来源的事实与数据（每条标注来源与时点），'
            '不做最终结论，不下投资判断。'),
        'iters': 4,
    },
    {
        'name': '量化建模师',
        'focus': (
            '你是量化建模师。职责：围绕用户任务做量化验证与测算——'
            '估值对比（stock_valuation / financial_ratios）、必要的历史数据检验'
            '（describe / correlation / adf_test 等，有数据文件时）。'
            '输出【量化结论】：每个测算给出方法、输入数据、计算结果与解读；'
            '数据不足的测算明确标注"待补充"，严禁编造数字。'),
        'iters': 4,
    },
    {
        'name': '风控合规官',
        'focus': (
            '你是风控合规官。职责：围绕用户任务做风险与合规审查——'
            '①数据口径一致性（复权/年化/TTM/合并报表）；'
            '②风险因素完整性（市场/政策/流动性/技术/地缘风险）；'
            '③合规表述（免责声明、不承诺收益、不荐股）。'
            '本阶段聚焦审查要点，不重新展开大范围取数（至多 1-2 次核验工具调用）。'
            '输出【风险与合规清单】+ 终稿修订要点。'),
        'iters': 2,
    },
]


def _stage_message(message: str, stage: Dict[str, Any], stage_idx: int,
                   outputs: List[Dict[str, str]], total: int = None) -> str:
    # total = 本任务实际命中的阶段数（阶段按需命中后不一定是 3），编号必须与之对齐，
    # 否则单阶段任务会对模型谎称"第 1/3 阶段"，诱发它去猜另外两个阶段的存在。
    total = total or len(STAGE_DEFS)
    parts = [
        message,
        f"\n\n# 多Agent协作 · 当前阶段：{stage['name']}"
        f"（第 {stage_idx + 1}/{total} 个执行阶段，本任务共命中 {total} 个阶段）\n"
        f"{stage['focus']}",
    ]
    if outputs:
        prev = "\n\n".join(
            f"## 上一阶段产出（{o['stage']}）\n{o['output'][:2500]}" for o in outputs)
        parts.append("\n\n# 前序阶段产出（你的输入）\n" + prev)
    return "\n".join(parts)


def run_pipeline_stream(message: str, llm_config: dict = None, current_file: str = None,
                        session_id: str = None, pipeline_models: dict = None):
    """三阶段并行协作管线（流式生成器）：
    三阶段同时点火（各阶段独立 ReAct 循环：线程 + 共享队列 + 心跳），并行模式下阶段间
    不传递产出，全部完成后由终稿合成汇总；
    事件与 ai_assistant._stream_agent_analysis 同构：status/progress/ping/done。

    pipeline_models: {阶段名: {provider, model}}，为对应阶段指定不同大模型；
    为 None/缺 stage 时该阶段沿用 llm_config（老路径零回归）。"""
    HEARTBEAT_S = 15
    t0 = time.time()

    # ---- 结构化追踪：管线是"一次用户请求 = 一条时间线"的根节点 ----
    # 三个阶段的 agent_loop 通过 trace_run_id 复用同一个 run（见 ai_agent.agent_loop），
    # 否则一次请求会碎成多条互不相干的轨迹，"哪一阶段挂了"无法在同一时间线上看出。
    _trace_id = None
    _trace_event = None
    _end_run = None
    try:
        from trace_store import begin_run as _begin_run, event as _trace_event_, end_run as _end_run_
        _trace_event, _end_run = _trace_event_, _end_run_
        _trace_id = (_begin_run(session_id, message, llm_config, 'agent_pipeline') or {}).get('run_id')
    except (Exception, SystemExit) as _te:
        logger.debug('管线 trace 初始化跳过: %s', _te)

    def _t_ev(etype, data=None):
        """管线埋点：属旁路可观测性，任何失败都不得影响主流程。"""
        if not _trace_id or _trace_event is None:
            return
        try:
            _trace_event(_trace_id, etype, data or {})
        except (Exception, SystemExit):
            pass

    # ---- 阶段按需命中（2026-09-18 晚）：只跑本任务真正需要的阶段 ----
    # 纯行业研究 → 仅市场分析师；估值/统计测算 → 仅量化建模师；
    # 合规/风险审查 → 仅风控合规官；"全面分析/完整分析/尽调" → 三阶段全跑。
    stages_active = [s for s in STAGE_DEFS
                     if s['name'] in select_pipeline_stages(message, current_file)]
    n_stages = len(stages_active)
    _sep = ' ∥ ' if n_stages > 1 else ''
    yield {'type': 'status',
           'message': (f'🤝 多Agent{"协作" if n_stages > 1 else "分析"}启动：命中 {n_stages} 个阶段'
                       f'（{_sep.join(s["name"] for s in stages_active)}）')}
    _t_ev('pipeline.stage_select', {
        'selected': [s['name'] for s in stages_active], 'n': n_stages,
        'all_stages': [s['name'] for s in STAGE_DEFS], 'routing': _STAGE_ROUTING})

    pm_map = pipeline_models or {}
    all_calls: List[Dict] = []
    usage_total = {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}

    from ai_agent import (agent_loop, is_cancelled as _is_cancelled,
                          cancel_session as _cancel_session)

    # 共享事件队列：元素为 {'_stage': 阶段名, ...事件字段}；阶段收尾投递 type='_stage_done'。
    evq: queue.Queue = queue.Queue()
    stage_runs: List[Dict[str, Any]] = []

    # 先逐阶段透出 status（保持"正在用哪个模型"对用户可见），再同时点火各阶段线程。
    # 并行模式下各阶段独立对原始用户任务负责，阶段间不再逐级传递产出（交叉验证由终稿合成完成）。
    _launch = '并行启动' if n_stages > 1 else '启动'
    for idx, stage in enumerate(stages_active):
        stage_cfg = _stage_llm_config(llm_config, stage['name'], pm_map)
        _prov = str((stage_cfg or {}).get('provider', '')) or 'default'
        _mdl = str((stage_cfg or {}).get('model', '')) or ''
        yield {'type': 'status',
               'message': f'阶段 {idx + 1}/{n_stages}：{stage["name"]}（{_prov}/{_mdl}）{_launch}...'}
        stage_msg = _stage_message(message, stage, idx, [], total=n_stages)  # 并行：无前序产出可传
        # 每阶段派生独立 session_id：隔离 LangGraph thread_id / 进度 tracker /
        # 取消标记 / checkpoint 文件，避免三个并发 agent_loop 写撞同一会话槽位。
        stage_sid = f"{session_id}#p{idx + 1}" if session_id else None
        _t_ev('pipeline.stage_start', {
            'stage': stage['name'], 'index': idx + 1,
            'provider': _prov, 'model': _mdl, 'iters': stage.get('iters'),
            'parallel': True,
            'fast': _STAGE_FAST,
            'tools': len(_STAGE_TOOL_WHITELIST.get(stage['name']) or [])})

        holder: Dict[str, Any] = {}

        def cb(evt, _name=stage['name']):
            try:
                _e = dict(evt)
                _e['_stage'] = _name
                evq.put(_e)
            except Exception:
                pass

        def attempt(cfg, h, _msg=stage_msg, _iters=stage['iters'],
                    _name=stage['name'], _cb=cb,
                    _tools=tuple(_STAGE_TOOL_WHITELIST.get(stage['name']) or ())):
            try:
                # 阶段提速三件套（PIPELINE_STAGE_FAST=0 时全部回退为改造前行为）：
                #   no_plan    —— 管线已给定角色与产出要求，跳过 legacy+LangGraph 两处规划
                #   tool_names —— 只下发本阶段职责所需的工具（省掉每轮重发的 tool schema）
                #   slim_prompt—— 跳过技能正文/知识点库/领域框架等重注入
                _fast = ({'no_plan': True, 'tool_names': list(_tools), 'slim_prompt': True}
                         if _STAGE_FAST else {})
                h['r'] = agent_loop(
                    user_message=_msg, llm_config=cfg, file_name=current_file,
                    max_iterations=_iters, session_id=stage_sid, progress_callback=_cb,
                    trace_run_id=_trace_id, **_fast)
            except Exception as e:
                logger.warning("管线阶段 %s 失败: %s", _name, e)
                h['r'] = {'ok': False, 'error': str(e)}
            finally:
                evq.put({'_stage': _name, 'type': '_stage_done'})

        threading.Thread(target=attempt, args=(stage_cfg, holder), daemon=True).start()
        stage_runs.append({'name': stage['name'], 'idx': idx, 'holder': holder,
                           'sid': stage_sid, 't0': time.time(),
                           'provider': _prov, 'model': _mdl})

    # 主循环：排空共享事件队列，直至本任务命中的全部阶段收尾。
    pending = {r['name'] for r in stage_runs}
    stage_meta: List[Dict[str, Any]] = []
    outputs_map: Dict[str, Dict[str, str]] = {}
    _cancels_pushed = False
    while pending:
        # 取消联动：用户对基础 session 发出取消 → 传导到各阶段派生会话
        if session_id and not _cancels_pushed and _is_cancelled(session_id):
            for r in stage_runs:
                if r['sid']:
                    _cancel_session(r['sid'])
            _cancels_pushed = True
        try:
            evt = evq.get(timeout=HEARTBEAT_S)
        except queue.Empty:
            yield {'type': 'ping', 'data': {'elapsed_s': HEARTBEAT_S}}
            continue
        if evt.get('type') == '_stage_done':
            name = evt.get('_stage')
            run = next((r for r in stage_runs if r['name'] == name), None)
            r = ((run or {}).get('holder') or {}).get('r') or {}
            u = r.get('usage') or {}
            for k in usage_total:
                usage_total[k] += int(u.get(k) or 0)
            reply = (r.get('output') or '').strip()
            # 失败归因：error 为空时回落 agent_loop 的 end_status（finalize_failed /
            # empty_output / degraded / error / cancelled），否则前端只看到
            # "阶段失败" 而看不到为什么（agent_loop 的 result 契约不含 error 时尤其如此）。
            _err = str(r.get('error') or r.get('end_status') or '')[:200]
            # 阶段成功判据：有文字产出 **且** agent_loop 未显式报失败。
            # 只看 bool(reply) 会造成假成功——实测 429 连败时 agent_loop 返回 ok=False
            # 且 output 是一段"配额耗尽/触发限流"的降级提示（133 字），会被当成正常产出，
            # 健康面板完全看不出阶段其实废了（degraded 也不报）。
            _ok = bool(reply) and (r.get('ok') is not False)
            _elapsed = round(time.time() - run['t0'], 1) if run else 0.0
            # 失败阶段不得把降级提示文本当产出喂给终稿——否则终稿会写成
            # "因接口限流无法进行分析"的错误说明书，而不是用户要的报告。
            outputs_map[name] = {
                'stage': name,
                'output': reply if _ok else '（本阶段未产出文字，终稿按已有信息继续）'}
            stage_meta.append({
                'stage': name, 'chars': len(reply), 'ok': _ok,
                'provider': (run or {}).get('provider') or 'default',
                'model': (run or {}).get('model') or '',
                'elapsed_s': _elapsed,
                # 成功阶段不带错误原因（否则 end_status='done' 会被当成错误文案透出）
                'error': _err if not _ok else ''})
            _idx = ((run or {}).get('idx') or 0) + 1
            _t_ev('pipeline.stage_done' if _ok else 'pipeline.stage_error', {
                'stage': name, 'index': _idx, 'ok': _ok, 'chars': len(reply),
                'provider': (run or {}).get('provider') or 'default',
                'model': (run or {}).get('model') or '',
                'elapsed_s': _elapsed,
                # 成功阶段不写 error（否则 end_status='done' 会被当错误原因落进事件）
                'error': (_err or None) if not _ok else None})
            if _ok:
                yield {'type': 'status',
                       'message': f'✅ 阶段 {_idx}/{n_stages}「{name}」完成（{len(reply)} 字，用时 {_elapsed}s）'}
            else:
                yield {'type': 'status',
                       'message': f'⚠️ 阶段 {_idx}/{n_stages}「{name}」未产出'
                                  + (f'：{_err[:80]}' if _err else '')}
            pending.discard(name)
            continue
        if evt.get('type') == 'tool_end':
            all_calls.append({
                'stage': evt.get('_stage'), 'tool': evt.get('tool'),
                'params': evt.get('params'), 'status': evt.get('tool_status'),
                'elapsed_ms': evt.get('elapsed_ms'),
            })
        evt['stage'] = evt.pop('_stage', None)   # 前端进度面板可区分是哪个阶段的事件
        yield {'type': 'progress', 'data': evt}

    # 按固定阶段顺序输出（并行完成顺序不定，产出顺序必须稳定可复现）
    _stage_order = [s['name'] for s in stages_active]
    outputs = [outputs_map[n] for n in _stage_order if n in outputs_map]
    stage_meta.sort(key=lambda m: _stage_order.index(m['stage']))

    # ---- 终稿合成（首席分析师，无工具单次调用）----
    yield {'type': 'status', 'message': '合成终稿...'}
    final_text = None
    _t_final = time.time()
    _final_cfg = _stage_llm_config(llm_config, '终稿合成', pm_map)
    _t_ev('pipeline.stage_start', {
        'stage': '终稿合成', 'index': n_stages + 1,
        'provider': str((_final_cfg or {}).get('provider', '')),
        'model': str((_final_cfg or {}).get('model', '')), 'iters': 1})
    try:
        from llm_client import chat
        stage_cfg = dict(_final_cfg or {})
        # 2026-09-18 晚修复：终稿合成固定用 glm-5.2（reasoning 系）后，若不关思考，
        # 思考内容会独占 max_tokens 预算 → 正文只剩开头几十字（实测 29 字符：
        # "# 宁德时代（300750.SZ）综合研究报告\n\n**首席"）。与单体 agent 终稿口径对齐
        # （ai_agent._lg_finish 同样只对 sensenova 设 reasoning_effort=none）。
        if str(stage_cfg.get('provider') or 'sensenova') == 'sensenova':
            stage_cfg['reasoning_effort'] = 'none'
        prev = "\n\n".join(f"## {o['stage']}\n{o['output'][:3000]}" for o in outputs)
        # 终稿 prompt 随命中阶段数动态生成：阶段按需命中后可能只有 1 个阶段，
        # 若仍写死"把三个协作阶段的产出合成"，会让模型去补并不存在的另外两个阶段（编造来源）。
        _names_txt = '、'.join(s['name'] for s in stages_active)
        if n_stages > 1:
            _sys = (f'你是首席分析师。把下列 {n_stages} 个协作阶段（{_names_txt}）的产出合成为一份'
                    '结构化的最终报告：按阶段维度组织，去重、统一口径，关键数字保留并注明来源阶段；'
                    '所有数字必须来自阶段产出，禁止编造；结尾附风险提示与免责声明。')
        else:
            _sys = (f'你是首席分析师。基于「{_names_txt}」阶段的产出撰写一份结构化的最终研究报告：'
                    '结构清晰、结论明确，关键数字保留并注明来源；'
                    '所有数字必须来自阶段产出，禁止编造；结尾附风险提示与免责声明。')
        resp = chat(
            [{'role': 'system', 'content': _sys},
             {'role': 'user', 'content': f"用户任务：{message}\n\n阶段产出：\n\n{prev}"}],
            stage_cfg, max_tokens=4800)
        final_text = (resp.get('content') or '').strip() if isinstance(resp, dict) else ''
    except Exception as e:
        logger.warning("管线终稿合成失败: %s", e)
    if not final_text:
        final_text = "\n\n".join(
            f"【{o['stage']}】\n{o['output'][:1500]}" for o in outputs if o['output'] and '未产出' not in o['output'])

    # 阶段成败聚合：只要有阶段未产出即标记 degraded，前端据此提示。
    # 此前 `ok` 恒为 True，即使三阶段全部失败也对外报"成功"，属"假成功"。
    _failed_stages = [m['stage'] for m in stage_meta if not m['ok']]
    _final_ok = bool((final_text or '').strip())
    _t_ev('pipeline.stage_done' if _final_ok else 'pipeline.stage_error', {
        'stage': '终稿合成', 'index': n_stages + 1, 'ok': _final_ok,
        'chars': len(final_text or ''), 'elapsed_s': round(time.time() - _t_final, 1),
        'error': None if _final_ok else '终稿为空（已回退为阶段产出拼接）'})
    if _end_run is not None and _trace_id:
        try:
            _end_run(_trace_id, 'ok' if _final_ok else 'degraded', summary={
                'iterations': n_stages + 1,
                'tool_calls': len(all_calls),
                'errors': sum(1 for m in stage_meta if not m['ok']),
                'total_tokens': usage_total.get('total_tokens'),
                'elapsed_ms': int((time.time() - t0) * 1000),
                'output_len': len(final_text or ''),
                'failed_stages': _failed_stages,
            })
        except (Exception, SystemExit):
            pass
    # 按需命中后阶段数不固定，推理路径必须如实反映本次真正跑了哪些阶段（前端"推理路径"展示用）
    _reason_txt = ' ∥ '.join(f"{m['stage']}·{m['model']}" for m in stage_meta)
    yield {'type': 'done', 'data': {
        'ok': bool((final_text or '').strip()),
        'reply': final_text,
        'source': 'agent_pipeline',
        'reasoning': ((f'{n_stages} 阶段并行协作（{_reason_txt}）→ 首席合成') if n_stages > 1
                      else f'单阶段深度分析（{_reason_txt}）→ 首席合成'),
        'stage_plan': {'selected': [s['name'] for s in stages_active], 'n': n_stages,
                       'all_stages': [s['name'] for s in STAGE_DEFS]},
        'calls': all_calls,
        'usage': usage_total,
        'elapsed_ms': int((time.time() - t0) * 1000),
        'stages': stage_meta,
        'degraded': bool(_failed_stages),
        'end_status': ('partial' if _failed_stages else 'ok'),
        'failed_stages': _failed_stages,
        'intent': {'action': 'analyze_data', 'params': {'file': current_file},
                   'confidence': 0.9, 'raw_text': message},
        'plan': {'steps': [], 'context': {}},
    }}
