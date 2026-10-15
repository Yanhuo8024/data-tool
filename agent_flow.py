#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_flow.py - Agent 流程可视化与故障定位（归一化层）
=====================================================
职责边界：`trace_store` 负责「记录」（落盘扁平 JSONL 事件流），本模块负责「看懂」。

为什么需要这一层：
  trace_store 的产物是 `{seq, type, data}` 的**时间序扁平列表**。前端若直接消费，
  必须自行推断「哪些事件属于同一阶段」「哪一步是失败的根因」「耗时花在哪」——
  这些推断逻辑一旦散落到前端，就无法复用、无法测试、也无法与后端降级语义对齐。
  故统一收敛到本模块，对外只暴露「阶段 → 节点」的两级视图 + 诊断结论。

对外主入口：
  - build_flow(run_id)        -> 结构化流程图（含诊断），可视化接口直接消费
  - diagnose_run(run_id)      -> 只要诊断结论（轻量）
  - summarize_recent(limit)   -> 最近若干次运行的健康概览（列表页）
  - aggregate_health(limit)   -> 各环节失败率排行（"哪一部分最常出问题"）

设计约束：
  1. 纯只读、零网络、零额度。任何异常都必须降级为「可读的空结果」，不得抛出到路由层。
  2. 事件类型未知时不得丢弃——归入 `other` 阶段，保证「有事件必有节点」。
  3. 诊断规则集中在 `_RULES`，新增问题类型必须同时补 `_SEVERITY_PENALTY` 与回归测试
     （tests/test_agent_flow.py::test_rule_table_complete）。
"""
import json
import logging
from datetime import datetime
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# 阈值常量（改这里即可调优诊断灵敏度）
# ----------------------------------------------------------------------
STALE_RUN_SECONDS = 600        # 无 run_end 且超过此时长未更新 → 判定为"中断"
SLOW_STAGE_MS = 30000          # 单阶段耗时超过此值 → 标记为瓶颈
SLOW_LLM_CALL_MS = 20000       # 单次 LLM 调用超过此值 → 标记为慢调用
FALLBACK_WARN_N = 2            # fallback_retry 达到此次数 → 提示原生工具未被采纳
FACT_UNVERIFIED_WARN = 0.5     # 事实溯源**未核验率**高于此值 → 提示数字缺出处
                               # （ai_agent._audit_fact_sources 的 ratio = missing/total，2026-09-18 修正：
                               #  旧实现误当"通过率"用，阈值方向反了——70% 未核验反而不报警、30% 未核验却误报）
MIN_TOOL_CALLS = 1             # 期望至少发生一次工具调用（否则视为"未取数"）

# 严重度扣分（health score 由 100 起扣）
_SEVERITY_PENALTY = {'critical': 25, 'warning': 10, 'info': 3}
_SEVERITY_ORDER = {'critical': 0, 'warning': 1, 'info': 2}

# 阶段定义（顺序即泳道展示顺序）
PHASES: List[Tuple[str, str]] = [
    ('intake', '入口与配置'),
    ('retrieval', '知识检索'),
    ('planning', '任务规划'),
    ('execution', 'LLM 与工具执行'),
    ('pipeline', '多Agent协作'),
    ('audit', '事实审计'),
    ('finalize', '终稿与收尾'),
    ('other', '其他事件'),
]
_PHASE_LABEL = dict(PHASES)

# 事件类型 → 阶段
_KIND_PHASE = {
    'run_start': 'intake',
    'agent_config': 'intake',
    'kb_retrieval': 'retrieval',
    'planner.start': 'planning',
    'planner.done': 'planning',
    'planner.error': 'planning',
    'llm_call': 'execution',
    'tool_result': 'execution',
    'fallback_retry': 'execution',
    'native_tools_degraded': 'execution',
    'tool_param_drop': 'execution',
    'pipeline.stage_config': 'pipeline',
    'pipeline.stage_start': 'pipeline',
    'pipeline.stage_done': 'pipeline',
    'pipeline.stage_error': 'pipeline',
    'fact_audit': 'audit',
    'finalize.start': 'finalize',
    'finalize.done': 'finalize',
    'finalize_failed': 'finalize',
    'run_end': 'finalize',
}

# 事件类型 → 可读标签
_KIND_LABEL = {
    'run_start': '运行开始',
    'agent_config': 'Agent 配置',
    'kb_retrieval': '知识库检索',
    'planner.start': '规划开始',
    'planner.done': '规划完成',
    'planner.error': '规划失败',
    'llm_call': 'LLM 调用',
    'tool_result': '工具执行',
    'fallback_retry': '分步守卫重试',
    'native_tools_degraded': '原生工具降级',
    'tool_param_drop': '工具参数丢弃',
    'tool_param_dropped': '工具参数丢弃',
    'pipeline.stage_config': '阶段配置',
    'pipeline.stage_start': '阶段开始',
    'pipeline.stage_done': '阶段完成',
    'pipeline.stage_error': '阶段失败',
    'fact_audit': '事实溯源审计',
    'finalize.start': '终稿合成开始',
    'finalize.done': '终稿合成完成',
    'finalize_failed': '终稿合成失败',
    'run_end': '运行结束',
}

_FAIL_STATUS = {'error', 'failed', 'fail', 'timeout', 'exception'}


# ----------------------------------------------------------------------
# 时间解析
# ----------------------------------------------------------------------
def _parse_ts(raw: Any) -> Optional[float]:
    """把 ISO 时间串解析为 epoch 秒；失败返回 None。"""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw)).timestamp()
    except Exception:
        return None


def _ms(delta_s: Optional[float]) -> Optional[int]:
    if delta_s is None:
        return None
    return int(round(delta_s * 1000))


# ----------------------------------------------------------------------
# 节点构造
# ----------------------------------------------------------------------
def _node_status(kind: str, data: Dict[str, Any]) -> str:
    """由事件内容推断节点状态：ok / warn / fail / skip。"""
    if kind in ('planner.error', 'finalize_failed', 'pipeline.stage_error'):
        return 'fail'
    if kind == 'finalize.done':
        return 'ok' if data.get('ok', True) else 'fail'
    if kind == 'tool_result':
        st = str(data.get('status') or '').lower()
        return 'fail' if st in _FAIL_STATUS else 'ok'
    if kind == 'pipeline.stage_done':
        return 'ok' if data.get('ok', True) else 'fail'
    if kind == 'run_end':
        outcome = str(data.get('outcome') or '')
        return 'ok' if data.get('ok') else ('skip' if outcome == 'cancelled' else 'fail')
    if kind in ('fallback_retry', 'native_tools_degraded', 'tool_param_drop', 'tool_param_dropped'):
        return 'warn'
    if kind == 'llm_call':
        return 'warn' if (data.get('duration_ms') or 0) > SLOW_LLM_CALL_MS else 'ok'
    if kind == 'fact_audit':
        try:
            # ratio = 未核验率（missing/total）：高于阈值才报警（2026-09-18 修正方向）
            if float(data.get('ratio')) > FACT_UNVERIFIED_WARN:
                return 'warn'
        except Exception as e:
            logger.debug('ratio 缺失/非数值，按 healthy 处理: %s', e)
        return 'ok'
    return 'ok'


def _node_label(kind: str, data: Dict[str, Any], idx: int) -> str:
    """生成人类可读的节点标题。"""
    base = _KIND_LABEL.get(kind, kind)
    if kind == 'llm_call':
        it = data.get('iteration')
        return f'{base} · 第 {it} 轮' if it is not None else base
    if kind == 'tool_result':
        tool = data.get('tool') or '未知工具'
        return f'{base} · {tool}'
    if kind in ('pipeline.stage_start', 'pipeline.stage_done', 'pipeline.stage_error'):
        stage = data.get('stage') or data.get('name') or ''
        return f'{base} · {stage}' if stage else base
    if kind == 'pipeline_stage':
        return base
    return base


def _node_detail(kind: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """裁剪事件 data，只保留可视化需要的字段（避免把大 preview 全塞给前端）。"""
    d: Dict[str, Any] = {}
    if kind == 'run_start':
        d = {'provider': data.get('provider'), 'model': data.get('model'),
             'user_message': str(data.get('user_message') or '')[:200],
             'skill': data.get('skill')}
    elif kind == 'agent_config':
        d = {'max_iterations': data.get('max_iterations'), 'plan': data.get('plan'),
             'skill': data.get('skill'), 'resume': data.get('resume'),
             'file': data.get('file')}
    elif kind == 'llm_call':
        d = {'iteration': data.get('iteration'), 'native': data.get('native'),
             'has_tool_calls': data.get('has_tool_calls'), 'content_len': data.get('content_len'),
             'duration_ms': data.get('duration_ms'), 'usage': data.get('usage')}
    elif kind == 'tool_result':
        d = {'tool': data.get('tool'), 'status': data.get('status'),
             'duration_ms': data.get('duration_ms'), 'cached': data.get('cached'),
             'iteration': data.get('iteration'),
             'preview': str(data.get('preview') or '')[:300],
             'params': data.get('params') if isinstance(data.get('params'), dict) else None}
    elif kind == 'planner.done':
        steps = data.get('steps') or []
        d = {'steps': steps if isinstance(steps, list) else [], 'step_count': len(steps) if isinstance(steps, list) else 0}
    elif kind == 'planner.start':
        d = {'user_goal': str(data.get('user_goal') or '')[:200]}
    elif kind == 'kb_retrieval':
        d = {k: v for k, v in data.items() if not isinstance(v, (dict, list)) or k in ('top', 'hits')}
    elif kind == 'fact_audit':
        d = {'total': data.get('total'), 'unverified': data.get('unverified'),
             'verified': data.get('verified'), 'ratio': data.get('ratio'),
             'policy': data.get('policy'), 'real_data_only': data.get('real_data_only'),
             'sources': data.get('sources') if isinstance(data.get('sources'), list) else None}
        vd = data.get('verified_data')
        ud = data.get('unverified_data')
        if isinstance(vd, list):
            d['verified_sample'] = [str(x.get('value')) for x in vd[:5] if isinstance(x, dict)]
        if isinstance(ud, list):
            d['unverified_sample'] = [str(x.get('value')) for x in ud[:5] if isinstance(x, dict)]
    elif kind in ('finalize.start', 'finalize.done'):
        # force_final = 达到最大轮数后强制收尾（正常流程，非错误）；
        # truncated = 终稿真的因 max_tokens 触顶被截断（2026-09-18 拆分语义，
        # 旧事件把 force_final 误标成 truncated，造成"终稿被截断"假警报）。
        d = {'ok': data.get('ok'), 'round': data.get('round'),
             'content_len': data.get('content_len'), 'duration_ms': data.get('duration_ms')}
        if 'force_final' in (data or {}):          # 新事件：语义已拆分
            d['force_final'] = data.get('force_final')
            d['truncated'] = data.get('truncated')
        else:                                       # 旧事件：truncated 实为 force_final
            d['force_final'] = data.get('truncated')
    elif kind in ('pipeline.stage_start', 'pipeline.stage_done', 'pipeline.stage_error'):
        d = {'stage': data.get('stage') or data.get('name'), 'ok': data.get('ok'),
             'elapsed_s': data.get('elapsed_s'), 'provider': data.get('provider'),
             'model': data.get('model'), 'chars': data.get('chars')}
    else:
        # 未知类型：原样保留标量字段，保证"有事件必有可读内容"
        d = {k: v for k, v in data.items() if isinstance(v, (str, int, float, bool)) or v is None}
        d = {k: (str(v)[:300] if isinstance(v, str) else v) for k, v in d.items()}
    if data.get('error'):
        d['error'] = str(data['error'])[:500]
    return d


def _build_nodes(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把事件流转换为有序节点列表（带相对偏移与耗时）。"""
    nodes: List[Dict[str, Any]] = []
    t0 = None
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ts = _parse_ts(ev.get('ts'))
        if ts is not None and t0 is None:
            t0 = ts
        kind = str(ev.get('type') or 'unknown')
        data = ev.get('data') if isinstance(ev.get('data'), dict) else {}
        seq = ev.get('seq') or 0
        phase = _KIND_PHASE.get(kind, 'other')

        # 自身耗时：优先显式字段，其次 elapsed_s（管线阶段用它）
        own_ms = None
        for key in ('duration_ms',):
            try:
                if data.get(key) is not None:
                    own_ms = int(float(data[key]))
                    break
            except Exception as e:
                logger.debug('duration_ms 解析失败，回落 elapsed_ms: %s', e)
                continue
        if own_ms is None and data.get('elapsed_ms') is not None:
            try:
                own_ms = int(float(data['elapsed_ms']))
            except Exception:
                own_ms = None
        if own_ms is None and data.get('elapsed_s') is not None:
            try:
                own_ms = int(float(data['elapsed_s']) * 1000)
            except Exception:
                own_ms = None

        nodes.append({
            'id': f'n{seq}',
            'seq': seq,
            'kind': kind,
            'phase': phase,
            'label': _node_label(kind, data, len(nodes)),
            'status': _node_status(kind, data),
            'ts': ev.get('ts'),
            'offset_ms': _ms(ts - t0) if (ts is not None and t0 is not None) else None,
            'elapsed_ms': own_ms,
            'elapsed_estimated': False,
            'error': str(data.get('error'))[:500] if data.get('error') else None,
            'detail': _node_detail(kind, data),
        })

    # 耗时推定：无显式耗时的节点，用「到下一个节点的间隔」近似，
    # 否则时间线上一串零宽标记无法反映真实等待（LLM 往返就是这类节点）。
    for i, nd in enumerate(nodes):
        if nd['elapsed_ms'] is None:
            nxt = nodes[i + 1] if i + 1 < len(nodes) else None
            if (nxt is not None and nd['offset_ms'] is not None
                    and nxt['offset_ms'] is not None and nxt['offset_ms'] >= nd['offset_ms']):
                nd['elapsed_ms'] = nxt['offset_ms'] - nd['offset_ms']
                nd['elapsed_estimated'] = True
            else:
                nd['elapsed_ms'] = 0
    return nodes


def _build_phases(nodes: List[Dict[str, Any]], total_ms: int = None) -> List[Dict[str, Any]]:
    """把节点按阶段归并成泳道，并汇总每阶段的状态与耗时。

    total_ms：整轮总耗时。最后一个阶段（含末尾节点的那个）用它收边 ——
    末尾节点是 run_end，自身耗时为 0，靠"到下一节点的间隔"推算会得到 0，
    会让收尾阶段看起来完全不花时间，与用户体感不符。
    """
    buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k, _ in PHASES}
    for nd in nodes:
        buckets.setdefault(nd['phase'], []).append(nd)

    phases: List[Dict[str, Any]] = []
    for key, label in PHASES:
        items = buckets.get(key) or []
        if not items:
            continue
        offsets = [n['offset_ms'] for n in items if n['offset_ms'] is not None]
        # 阶段状态 = 组内最严重者（fail > warn > ok > skip）
        rank = {'fail': 0, 'warn': 1, 'ok': 2, 'skip': 3}
        status = min((n['status'] for n in items), key=lambda s: rank.get(s, 9))
        elapsed = None
        if offsets:
            last = items[-1]
            end = (last['offset_ms'] or 0) + (last['elapsed_ms'] or 0)
            if total_ms and nodes and last is nodes[-1]:
                end = max(end, int(total_ms))
            elapsed = max(0, end - min(offsets))
        phases.append({
            'key': key,
            'label': label,
            'status': status,
            'offset_ms': min(offsets) if offsets else None,
            'elapsed_ms': elapsed,
            'node_count': len(items),
            'node_ids': [n['id'] for n in items],
            'errors': [n['label'] for n in items if n['status'] == 'fail'][:5],
        })
    return phases


# ----------------------------------------------------------------------
# 运行级摘要
# ----------------------------------------------------------------------
def _run_metrics(events: List[Dict[str, Any]], nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """汇总一次运行的关键指标。"""
    run_start = next((e for e in events if e.get('type') == 'run_start'), None)
    run_end = next((e for e in events if e.get('type') == 'run_end'), None)
    cfg_ev = next((e for e in events
                   if e.get('type') in ('agent_config', 'pipeline.stage_config')), None)
    _file = str(((cfg_ev or {}).get('data') or {}).get('file') or '')
    sd = (run_start or {}).get('data') or {}
    ed = (run_end or {}).get('data') or {}
    summary = ed.get('summary') or {}

    tool_nodes = [n for n in nodes if n['kind'] == 'tool_result']
    llm_nodes = [n for n in nodes if n['kind'] == 'llm_call']
    failed_tools = [n for n in tool_nodes if n['status'] == 'fail']

    # 总耗时：优先用 run_end 落盘值，其次由时间轴推算
    elapsed = summary.get('elapsed_ms')
    if elapsed is None:
        offs = [n['offset_ms'] for n in nodes if n['offset_ms'] is not None]
        if offs:
            last = nodes[-1] if nodes else None
            end = (last['offset_ms'] or 0) + (last['elapsed_ms'] or 0) if last else max(offs)
            elapsed = max(0, end - min(offs))
    try:
        elapsed = int(elapsed) if elapsed is not None else None
    except Exception:
        elapsed = None

    return {
        'session_id': sd.get('session_id'),
        'user_message': str(sd.get('user_message') or '')[:400],
        'provider': sd.get('provider') or '',
        'model': sd.get('model') or '',
        'skill': sd.get('skill'),
        'file': _file,
        'has_file': bool(_file),
        'outcome': ed.get('outcome'),
        'declared_ok': ed.get('ok'),
        'iterations': summary.get('iterations'),
        'llm_calls': len(llm_nodes),
        'tool_calls': len(tool_nodes),
        'failed_tools': len(failed_tools),
        'total_tokens': summary.get('total_tokens'),
        'output_len': summary.get('output_len'),
        'elapsed_ms': elapsed,
        'fallback_retries': sum(1 for n in nodes if n['kind'] == 'fallback_retry'),
        'ended': run_end is not None,
        'started_at': (run_start or {}).get('ts'),
    }


def _overall_status(metrics: Dict[str, Any], events: List[Dict[str, Any]],
                    file_age_s: Optional[float]) -> str:
    """运行总体状态：ok | degraded | failed | running | interrupted。"""
    if not metrics['ended']:
        if file_age_s is not None and file_age_s > STALE_RUN_SECONDS:
            return 'interrupted'
        return 'running'
    if metrics['declared_ok']:
        return 'ok'
    outcome = str(metrics.get('outcome') or '')
    if outcome in ('cancelled', 'degraded', 'partial'):
        return 'degraded'
    return 'failed'


# ----------------------------------------------------------------------
# 诊断规则
# ----------------------------------------------------------------------
def _mk(code: str, severity: str, title: str, detail: str, hint: str = '',
        phase: str = '', evidence: List[int] = None) -> Dict[str, Any]:
    return {'code': code, 'severity': severity, 'title': title, 'detail': detail,
            'hint': hint, 'phase': phase, 'evidence': list(evidence or [])}


def _rule_run_interrupted(m, nodes, events, ctx) -> Optional[Dict]:
    """有 run_start 但无 run_end 且已停滞 → 不是"还在跑"，而是进程被打断。"""
    if m['ended']:
        return None
    age = ctx.get('file_age_s')
    if age is not None and age > STALE_RUN_SECONDS:
        return _mk('RUN_INTERRUPTED', 'critical', '运行未收尾（中断）',
                   f'轨迹只有 run_start 没有 run_end，且已 {int(age)}s 未更新。'
                   '常见原因：进程被重启/杀死、请求超时、或收尾路径抛异常未落 run_end。',
                   '检查该时段服务日志是否有重启或未捕获异常；若为超时，可调大 AGENT_DEADLINE。',
                   phase='finalize', evidence=[n['seq'] for n in nodes if n['kind'] == 'run_start'])
    return None


def _rule_run_failed(m, nodes, events, ctx) -> Optional[Dict]:
    if m['ended'] and not m['declared_ok'] and str(m.get('outcome') or '') not in ('cancelled',):
        return _mk('RUN_FAILED', 'critical', '运行失败',
                   f'run_end 标记 ok=false（outcome={m.get("outcome")}）。',
                   '按下方更具体的问题定位根因；若无更具体项，查看 run_end 的 summary.error。',
                   phase='finalize')
    return None


def _rule_graph_raised(m, nodes, events, ctx) -> Optional[Dict]:
    """图执行抛错：summary.error == 'graph invoke raised'。"""
    for ev in events:
        if ev.get('type') != 'run_end':
            continue
        s = ((ev.get('data') or {}).get('summary') or {})
        if str(s.get('error') or '') == 'graph invoke raised':
            _detail = ''
            if s.get('error_type') or s.get('error_detail'):
                _detail = '（%s: %s）' % (
                    s.get('error_type') or '?', s.get('error_detail') or '')
            return _mk('GRAPH_INVOKE_RAISED', 'critical', 'LangGraph 图执行抛错',
                       '编排主循环在 invoke 阶段抛出异常%s。'
                       '图中任一节点抛错都会走到这里，需结合服务日志定位到具体节点。' % _detail,
                       '重点排查 planner / agent / tools 三节点；旁路（trace/checkpoint）异常不会走到这里。',
                       phase='finalize', evidence=[ev.get('seq')])
    return None


def _rule_empty_output(m, nodes, events, ctx) -> Optional[Dict]:
    if m['ended'] and m['declared_ok'] and not (m.get('output_len') or 0):
        return _mk('EMPTY_OUTPUT', 'critical', '终稿为空',
                   '运行标记成功，但输出长度为 0 —— 用户会看到"AI 不回复"。'
                   '典型成因：reasoning 系模型的 max_tokens 被思考过程吃满，content 为空串。',
                   '确认 llm_client 的 reasoning 兜底未被移除；或调大 max_tokens。',
                   'finalize')
    return None


def _rule_planner_error(m, nodes, events, ctx) -> Optional[Dict]:
    bad = [n for n in nodes if n['kind'] == 'planner.error']
    if bad:
        return _mk('PLANNER_ERROR', 'warning', '任务规划失败',
                   f'planner 抛错 {len(bad)} 次：{(bad[0]["error"] or "")[:160]}',
                   '规划失败会使 Plan-and-Execute 静默降级为 plan=[]。'
                   '最常见原因是 planner 的 messages 未以 user 轮结束（部分 provider 会返回 400）。',
                   'planning', [n['seq'] for n in bad])
    return None


def _rule_finalize_failed(m, nodes, events, ctx) -> Optional[Dict]:
    bad = [n for n in nodes if n['kind'] == 'finalize_failed']
    if bad:
        return _mk('FINALIZE_FAILED', 'critical', '终稿合成失败',
                   f'finalize 节点抛错 {len(bad)} 次：{(bad[0]["error"] or "")[:160]}',
                   '终稿失败通常意味着本轮无有效输出；检查 LLM 是否被限流，或提示词是否超长。',
                   'finalize', [n['seq'] for n in bad])
    return None


def _rule_tool_errors(m, nodes, events, ctx) -> Optional[Dict]:
    bad = [n for n in nodes if n['kind'] == 'tool_result' and n['status'] == 'fail']
    if bad:
        names = []
        for n in bad:
            t = (n['detail'] or {}).get('tool') or '?'
            if t not in names:
                names.append(t)
        return _mk('TOOL_ERROR', 'warning', f'工具执行失败 {len(bad)} 次',
                   f'失败工具：{", ".join(names[:6])}',
                   '单个工具失败未必致命（agent 会换工具），但同一工具连续失败说明参数或数据源有问题。',
                   'execution', [n['seq'] for n in bad][:20])
    return None


def _rule_no_tool_calls(m, nodes, events, ctx) -> Optional[Dict]:
    """多轮 LLM 却一次工具都没调 → 很可能在"空想"，输出可信度低。

    必须分级，否则噪声会淹没真问题：有数据文件却零工具 = 该取数而没取；
    无文件时零工具多为闲聊/纯知识问答，属正常形态（降为 info）。
    """
    if not m['ended'] or not m.get('llm_calls'):
        return None
    if m['tool_calls'] or m['llm_calls'] < 2:
        return None
    if m.get('has_file'):
        return _mk('NO_TOOL_CALLS', 'warning', '有数据却未调用任何工具',
                   f'已挂载数据文件「{m.get("file")}」，但 {m["llm_calls"]} 次 LLM 调用中'
                   '工具调用为 0 —— 回答未基于真实数据。',
                   '检查意图路由是否把该问题误判为 chat；'
                   '或该模型未产出可解析的工具调用（原生 tools 采纳率偏低）。',
                   'execution')
    return _mk('NO_TOOL_CALLS_NOFILE', 'info', '未调用任何工具（无数据文件）',
               f'{m["llm_calls"]} 次 LLM 调用、零工具调用；本轮未挂载数据文件，'
               '属纯知识问答或闲聊的正常形态。',
               '若本应带文件分析，检查前端是否把 file 传给了 /api/assistant/chat。',
               'execution')


def _rule_fallback_retry(m, nodes, events, ctx) -> Optional[Dict]:
    n = m.get('fallback_retries') or 0
    if n >= FALLBACK_WARN_N:
        return _mk('NATIVE_FALLBACK', 'warning', '原生工具调用未被采纳',
                   f'触发分步守卫重试 {n} 次：模型虽带 tools 却未返回可解析的 tool_calls，'
                   '被迫回退到文本解析模式，额外增加 LLM 往返。',
                   '这是已知现象（采纳率极低）；若频繁出现可考虑关闭该 provider 的原生 tools。',
                   'execution', [x['seq'] for x in nodes if x['kind'] == 'fallback_retry'][:10])
    return None


def _rule_native_degraded(m, nodes, events, ctx) -> Optional[Dict]:
    bad = [n for n in nodes if n['kind'] == 'native_tools_degraded']
    if bad:
        d = bad[-1]['detail'] or {}
        return _mk('NATIVE_DEGRADED', 'info', '原生工具自动降级',
                   f'因连续未命中（misses={d.get("misses")}）已对该模型停用原生 tools。',
                   '属自适应保护，非故障；如需强制开启可设 AGENT_NATIVE_AUTO_DEGRADE=0。',
                   'execution', [n['seq'] for n in bad])
    return None


def _rule_fact_unverified(m, nodes, events, ctx) -> Optional[Dict]:
    for n in nodes:
        if n['kind'] != 'fact_audit':
            continue
        try:
            ratio = float((n['detail'] or {}).get('ratio'))
        except Exception as e:
            logger.debug('事实核验率缺失/非数值，跳过该节点: %s', e)
            continue
        # ratio = 未核验率（missing/total）：高于阈值才报警（2026-09-18 修正方向反转）
        if ratio > FACT_UNVERIFIED_WARN:
            _d = n['detail'] or {}
            return _mk('FACT_UNVERIFIED', 'warning', '数字缺少可核查来源',
                       f'事实核查未核验率 {ratio:.0%}'
                       f'（未验证 {_d.get("unverified")}/{_d.get("total")}）'
                       + (f'，已核验 {_d.get("verified")} 处' if _d.get('verified') is not None else '')
                       + '。数据必须真实：未核验数字不得作为结论依据。',
                       '仅成功工具的真实返回可被核验；请让 agent 先取数再写数，'
                       '或在报告中把无源数字标为「待补充」。',
                       'audit', [n['seq']])
    return None


def _rule_pipeline_stage_failed(m, nodes, events, ctx) -> Optional[Dict]:
    bad = [n for n in nodes if n['kind'] == 'pipeline.stage_error'
           or (n['kind'] == 'pipeline.stage_done' and n['status'] == 'fail')]
    if bad:
        names = [str((n['detail'] or {}).get('stage') or '?') for n in bad]
        return _mk('PIPELINE_STAGE_FAILED', 'critical', '多Agent协作阶段失败',
                   f'失败阶段：{", ".join(names[:6])}',
                   '该阶段的 provider/model 可能无凭据或被限流；可开启「自动选择大模型」自动轮换。',
                   'pipeline', [n['seq'] for n in bad])
    return None


def _rule_tool_param_drop(m, nodes, events, ctx) -> Optional[Dict]:
    bad = [n for n in nodes if n['kind'] in ('tool_param_drop', 'tool_param_dropped')]
    if bad:
        return _mk('TOOL_PARAM_DROP', 'info', '工具参数被丢弃',
                   f'模型产出的参数不在工具签名内，已丢弃 {len(bad)} 次。',
                   '属容错行为；若频繁出现说明该模型对工具 schema 遵循度低。',
                   'execution', [n['seq'] for n in bad][:10])
    return None


def _rule_kb_miss(m, nodes, events, ctx) -> Optional[Dict]:
    for n in nodes:
        if n['kind'] != 'kb_retrieval':
            continue
        d = n['detail'] or {}
        # 注意：不能写 `d.get('hits') or d.get('top') or d.get('count')` ——
        # hits=0（零命中）会被 `or` 链吞掉变成 None，导致最该报警的情况反而不报警。
        hits = None
        for _k in ('hits', 'top', 'count'):
            if d.get(_k) is not None:
                hits = d[_k]
                break
        if hits in (0, [], {}):
            return _mk('KB_MISS', 'info', '知识库零命中',
                       '本轮 RAG 检索未命中任何条目，回答将完全依赖模型自身知识或联网结果。',
                       '若该问题本应命中知识库，检查知识库是否已录入对应专题。',
                       'retrieval', [n['seq']])
    return None


def _rule_slow(m, nodes, events, ctx) -> Optional[Dict]:
    """耗时瓶颈：总时长占比最高的阶段。"""
    phases = ctx.get('phases') or []
    cands = [p for p in phases if (p.get('elapsed_ms') or 0) >= SLOW_STAGE_MS]
    if not cands:
        return None
    worst = max(cands, key=lambda p: p.get('elapsed_ms') or 0)
    return _mk('SLOW_STAGE', 'info', f'耗时集中在「{worst["label"]}」',
               f'{worst["label"]} 耗时 {round((worst["elapsed_ms"] or 0) / 1000, 1)}s'
               f'（占本次运行主要时间）。',
               '若为 LLM 调用，说明模型本身慢或正在被限流退避；节流间隔已自适应，可换更快的模型。',
               worst['key'])


def _rule_slow_llm(m, nodes, events, ctx) -> Optional[Dict]:
    slow = [n for n in nodes if n['kind'] == 'llm_call' and (n.get('elapsed_ms') or 0) >= SLOW_LLM_CALL_MS]
    if slow:
        worst = max(slow, key=lambda n: n['elapsed_ms'] or 0)
        return _mk('SLOW_LLM_CALL', 'info', '存在超慢的 LLM 调用',
                   f'{len(slow)} 次调用超过 {SLOW_LLM_CALL_MS // 1000}s，最慢 '
                   f'{round((worst["elapsed_ms"] or 0) / 1000, 1)}s（第 '
                   f'{(worst["detail"] or {}).get("iteration")} 轮）。',
                   '多为服务端拥塞或账户限速退避；两者性质不同，前者本地节流无效、只能换模型。',
                   'execution', [n['seq'] for n in slow][:10])
    return None


def _rule_truncated(m, nodes, events, ctx) -> Optional[Dict]:
    if ctx.get('summary', {}).get('truncated'):
        return _mk('TRUNCATED', 'warning', '输出被截断', '本轮回答达到长度上限被截断。',
                   '可提高 max_tokens 或让模型精简表达。', 'finalize')
    return None


def _rule_force_final(m, nodes, events, ctx) -> Optional[Dict]:
    """达到最大轮数后强制收尾：属正常流程（非截断），仅作 info 提示。
    2026-09-18：此前 force_final 被误标成 truncated，造成"终稿被截断"假警报。"""
    if ctx.get('summary', {}).get('force_final') and not ctx.get('summary', {}).get('truncated'):
        return _mk('FORCE_FINAL', 'info', '达到最大轮数自动收尾',
                   '本轮分析用满迭代预算后强制收尾生成终稿（正常流程，非截断）。',
                   '若频繁出现且结论覆盖不足，可为该类任务提高最大轮数。', 'finalize')
    return None


# 规则表：顺序即报告顺序。新增规则须同步补测试。
_RULES = (
    _rule_run_interrupted,
    _rule_graph_raised,
    _rule_run_failed,
    _rule_empty_output,
    _rule_finalize_failed,
    _rule_pipeline_stage_failed,
    _rule_planner_error,
    _rule_tool_errors,
    _rule_no_tool_calls,
    _rule_fallback_retry,
    _rule_fact_unverified,
    _rule_native_degraded,
    _rule_tool_param_drop,
    _rule_kb_miss,
    _rule_truncated,
    _rule_force_final,
    _rule_slow_llm,
    _rule_slow,
)


# 规则目录：code -> (严重度, 中文标题, 说明)。
# 与 _RULES 中的实际产出一一对应；测试 test_rule_catalog_matches_emitted_codes
# 会遍历真实诊断结果，确保不存在"产出了但目录里没有"的 code。
RULE_CATALOG: Dict[str, Tuple[str, str, str]] = {
    'RUN_INTERRUPTED': ('critical', '运行未收尾（中断）', '只有 run_start 没有 run_end，且已长时间未更新'),
    'GRAPH_INVOKE_RAISED': ('critical', 'LangGraph 图执行抛错', '编排主循环在 invoke 阶段抛出异常'),
    'RUN_FAILED': ('critical', '运行失败', 'run_end 标记 ok=false'),
    'EMPTY_OUTPUT': ('critical', '终稿为空', '标记成功但输出为空，用户会看到"AI 不回复"'),
    'FINALIZE_FAILED': ('critical', '终稿合成失败', 'finalize 节点抛错'),
    'PIPELINE_STAGE_FAILED': ('critical', '多Agent协作阶段失败', '某一协作阶段未产出内容'),
    'PLANNER_ERROR': ('warning', '任务规划失败', 'planner 抛错，计划降级为空'),
    'TOOL_ERROR': ('warning', '工具执行失败', '工具返回错误状态'),
    'NO_TOOL_CALLS': ('warning', '有数据却未调用任何工具', '挂了数据文件却零工具调用，回答未基于真实数据'),
    'NO_TOOL_CALLS_NOFILE': ('info', '未调用任何工具（无数据文件）', '纯知识问答 / 闲聊的正常形态'),
    'NATIVE_FALLBACK': ('warning', '原生工具调用未被采纳', '模型带 tools 却不返回 tool_calls，被迫回退文本解析'),
    'FACT_UNVERIFIED': ('warning', '数字缺少可核查来源', '事实溯源未核验率偏高，存在编造风险'),
    'TRUNCATED': ('warning', '输出被截断', '达到长度上限'),
    'FORCE_FINAL': ('info', '达到最大轮数自动收尾', '用满迭代预算后强制收尾（正常流程，非截断）'),
    'NATIVE_DEGRADED': ('info', '原生工具自动降级', '连续未命中后对该模型停用原生 tools（自适应保护）'),
    'TOOL_PARAM_DROP': ('info', '工具参数被丢弃', '模型产出的参数不在工具签名内'),
    'KB_MISS': ('info', '知识库零命中', 'RAG 检索未命中任何条目'),
    'SLOW_STAGE': ('info', '耗时集中在某阶段', '单阶段耗时显著高于阈值'),
    'SLOW_LLM_CALL': ('info', '存在超慢的 LLM 调用', '单次 LLM 往返耗时过高'),
}


def rule_catalog() -> List[Dict[str, Any]]:
    """返回诊断规则清单（供前端图例 / 文档展示）。"""
    return [{'code': c, 'severity': s, 'title': t, 'description': d}
            for c, (s, t, d) in sorted(RULE_CATALOG.items(),
                                       key=lambda kv: (_SEVERITY_ORDER.get(kv[1][0], 9), kv[0]))]


def _diagnose(metrics: Dict[str, Any], nodes: List[Dict[str, Any]],
              events: List[Dict[str, Any]], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """跑规则表，产出「哪一部分出了问题」的结论。

    规则统一签名 (metrics, nodes, events, ctx)；阶段信息从 ctx['phases'] 取。
    （曾因多一个位置参数导致 events 位置收到 phases，使依赖 events 的规则静默失效。）
    """
    problems: List[Dict[str, Any]] = []
    for rule in _RULES:
        try:
            p = rule(metrics, nodes, events, ctx)
        except Exception as e:      # 规则本身出错不得影响诊断可用性
            logger.debug('诊断规则 %s 异常: %s', getattr(rule, '__name__', rule), e)
            continue
        if p:
            problems.append(p)

    problems.sort(key=lambda p: (_SEVERITY_ORDER.get(p['severity'], 9), p.get('phase') or ''))

    score = 100
    for p in problems:
        score -= _SEVERITY_PENALTY.get(p['severity'], 5)
    score = max(0, min(100, score))

    counts = {'critical': 0, 'warning': 0, 'info': 0}
    for p in problems:
        if p['severity'] in counts:
            counts[p['severity']] += 1

    level = 'ok'
    if counts['critical']:
        level = 'error'
    elif counts['warning']:
        level = 'warn'

    return {
        'level': level,
        'score': score,
        'counts': counts,
        'primary_fault': problems[0] if problems else None,
        'problems': problems,
    }


# ----------------------------------------------------------------------
# 对外主入口
# ----------------------------------------------------------------------
def _load(run_id: str) -> Optional[Dict[str, Any]]:
    from trace_store import read_run, trace_file
    run = read_run(run_id)
    if run is None:
        return None
    age = None
    try:
        import os
        p = trace_file(run_id)
        if p and os.path.exists(p):
            import time
            age = max(0.0, time.time() - os.path.getmtime(p))
    except (Exception, SystemExit):
        age = None
    run['file_age_s'] = age
    return run


def build_flow(run_id: str, include_events: bool = False) -> Optional[Dict[str, Any]]:
    """把一次运行归一化为「阶段 + 节点 + 诊断」。run_id 不存在返回 None。"""
    run = _load(run_id)
    if run is None:
        return None
    events = [e for e in (run.get('events') or []) if isinstance(e, dict)]
    nodes = _build_nodes(events)
    metrics = _run_metrics(events, nodes)
    metrics['status'] = _overall_status(metrics, events, run.get('file_age_s'))
    # 先有总耗时才能算阶段耗时（末尾阶段需用它收边）
    phases = _build_phases(nodes, total_ms=metrics.get('elapsed_ms'))

    run_end = next((e for e in events if e.get('type') == 'run_end'), None)
    summary = ((run_end or {}).get('data') or {}).get('summary') or {}
    ctx = {'file_age_s': run.get('file_age_s'), 'phases': phases, 'summary': summary}
    diagnosis = _diagnose(metrics, nodes, events, ctx)

    out = {
        'run_id': run_id,
        'count': run.get('count') or len(events),
        'file': run.get('file'),
        'status': metrics['status'],
        'metrics': metrics,
        'phases': phases,
        'nodes': nodes,
        'diagnosis': diagnosis,
        'phase_defs': [{'key': k, 'label': v} for k, v in PHASES],
    }
    if include_events:
        out['events'] = events
    return out


def diagnose_run(run_id: str) -> Optional[Dict[str, Any]]:
    """轻量版：只要诊断结论（含运行级指标）。"""
    flow = build_flow(run_id, include_events=False)
    if flow is None:
        return None
    return {
        'run_id': run_id,
        'status': flow['status'],
        'metrics': flow['metrics'],
        'diagnosis': flow['diagnosis'],
        'failed_phases': [p['key'] for p in flow['phases'] if p['status'] == 'fail'],
    }


# ==================================================================
# 回放 / 分叉（P1，对标 DeepSeek Harness 的 resume / fork / replay）
# ==================================================================
# 为什么放在这一层：`trace_store` 提供的是**完整事件流**（append-only），
# 但"回到第 N 个事件时这次运行长什么样"需要重算节点/指标/诊断 —— 那正是本模块的职责。
# 因此回放不新建存储、不改写轨迹，只是**在已有事件流上做前缀重算**。
#
# 🚨 关键约束（改动前必读）：
#   1. **纯只读**：不得改动 `_seq_cache` / 不得写入任何文件。回放是"看"，不是"做"。
#   2. **前缀语义**：`at_seq` 之前的全部事件（含 at_seq）。闭区间，便于前端"点第 N 步看当时状态"。
#   3. 截断后的视图**不能**沿用完整运行的 `status` —— 否则一个跑到一半的窗口会显示
#      "失败/成功"，误导排查。故回放态统一标 `replay`（可带 `replay_partial`）。
#   4. 与 `agent_loop(resume=True)` 的区别：那个是**真恢复执行**（会继续跑 LLM），
#      本层只做**观测回放**（零额度、零副作用）。两者互补，勿混。


def _event_seq(e: Dict[str, Any]) -> int:
    """取事件 seq；缺失/非法时返回 0（排序时归到最前，不影响确定性）。"""
    try:
        return int(e.get('seq') or 0)
    except (TypeError, ValueError):
        return 0


def timeline(run_id: str) -> Optional[Dict[str, Any]]:
    """返回可逐帧步进的**轻量时间线**（供前端做进度条 / 单步前进后退）。

    每条含 {seq, type, ts, label, status}，不含 data 大字段 —— 避免把整个事件流
    塞给前端（一次 Agent 运行可达数百条、含完整 prompt）。
    """
    run = _load(run_id)
    if run is None:
        return None
    events = [e for e in (run.get('events') or []) if isinstance(e, dict)]
    events.sort(key=_event_seq)
    frames = []
    for i, e in enumerate(events):
        kind = e.get('type') or 'unknown'
        data = e.get('data') if isinstance(e.get('data'), dict) else {}
        frames.append({
            'seq': _event_seq(e),
            'type': kind,
            'ts': e.get('ts'),
            'label': _node_label(kind, data, i),
            'status': _node_status(kind, data),
        })
    return {
        'run_id': run_id,
        'count': len(frames),
        'frames': frames,
        'last_seq': _event_seq(events[-1]) if events else 0,
    }


def replay_from(run_id: str, at_seq: int, include_events: bool = False
                ) -> Optional[Dict[str, Any]]:
    """把运行**重放到第 `at_seq` 个事件为止**（闭区间前缀），返回当时的视图。

    用途：坏 Case 排查 —— "第 12 步时工具还没返回，模型为什么就下结论了？"
    返回结构与 `build_flow` 一致（nodes / metrics / phases / diagnosis），
    以便前端复用同一套渲染；额外带 `replay` 元信息说明截断位置。

    `at_seq` 超出范围时自动夹到 [0, last_seq]，不报错（前端拖进度条会越界）。
    """
    run = _load(run_id)
    if run is None:
        return None
    all_events = [e for e in (run.get('events') or []) if isinstance(e, dict)]
    all_events.sort(key=_event_seq)
    last = _event_seq(all_events[-1]) if all_events else 0
    try:
        cut = int(at_seq)
    except (TypeError, ValueError):
        cut = last
    cut = max(0, min(cut, last))

    events = [e for e in all_events if _event_seq(e) <= cut]
    nodes = _build_nodes(events)
    metrics = _run_metrics(events, nodes)
    # 截断视图不得沿用完整运行的终态：标成 replay（未收尾）/ replay_partial（已收尾但被截）
    _finished = any(e.get('type') == 'run_end' for e in events)
    metrics['status'] = 'replay_partial' if _finished else 'replay'
    phases = _build_phases(nodes, total_ms=metrics.get('elapsed_ms'))

    run_end = next((e for e in events if e.get('type') == 'run_end'), None)
    summary = ((run_end or {}).get('data') or {}).get('summary') or {}
    ctx = {'file_age_s': run.get('file_age_s'), 'phases': phases, 'summary': summary}
    diagnosis = _diagnose(metrics, nodes, events, ctx)

    out = {
        'run_id': run_id,
        'count': len(events),
        'file': run.get('file'),
        'status': metrics['status'],
        'metrics': metrics,
        'phases': phases,
        'nodes': nodes,
        'diagnosis': diagnosis,
        'phase_defs': [{'key': k, 'label': v} for k, v in PHASES],
        'replay': {
            'at_seq': cut,
            'from': 'start',
            'total_seq': last,
            'truncated': cut < last,
            'finished': _finished,
            'mode': 'observation',
            'note': '观测回放：零额度零副作用，不恢复执行。真恢复见 agent_loop(resume=True)。',
        },
    }
    if include_events:
        out['events'] = events
    return out


def fork_at(run_id: str, at_seq: int) -> Optional[Dict[str, Any]]:
    """给出**从第 `at_seq` 步分叉**所需的上下文包（对标 dsh 的 fork）。

    只是把"分叉点之前发生了什么"整理成可注入的摘要 —— **不执行任何恢复**。
    真正的重新执行由调用方决定（当前项目未接自动重跑，避免隐式消耗额度）。

    返回：{run_id, at_seq, user_message, injected_context, tool_calls, status_before}
      · `injected_context`：截断点前的工具调用摘要（工具名 + 参数 + 成败），
        可直接拼进新一轮的 user 消息，让模型"继承已知信息、从分叉点换条路走"。
    """
    flow = replay_from(run_id, at_seq, include_events=True)
    if flow is None:
        return None
    events = flow.get('events') or []
    start = next((e for e in events if e.get('type') == 'run_start'), None)
    calls = []
    for e in events:
        t = e.get('type')
        if t not in ('tool_call', 'tool_result'):
            continue
        d = e.get('data') if isinstance(e.get('data'), dict) else {}
        calls.append({
            'seq': _event_seq(e),
            'type': t,
            'tool': d.get('tool') or d.get('name') or '',
            'status': _node_status(t, d),
            'summary': str(d.get('params') or d.get('result') or '')[:300],
        })

    lines = []
    for c in calls:
        if c['type'] == 'tool_call':
            lines.append(f"- [{c['status']}] {c['tool']}({c['summary']})")
    return {
        'run_id': run_id,
        'at_seq': flow['replay']['at_seq'],
        'user_message': ((start or {}).get('data') or {}).get('user_message', ''),
        'tool_calls': calls,
        'injected_context': (
            '以下是本次任务迄今为止已完成的取数步骤（分叉点之前的上下文），'
            '请在此基础上继续，不要重复已成功且结果仍有效的调用：\n' + '\n'.join(lines)
            if lines else ''
        ),
        'status_before': flow['status'],
        'mode': 'fork_context_only',
        'note': '仅产出分叉上下文；未自动重跑（避免隐式消耗额度）。',
    }


def summarize_recent(limit: int = 30) -> List[Dict[str, Any]]:
    """最近若干次运行的概览（供列表页）。每条含健康态与首要故障，便于一眼定位坏例。"""
    from trace_store import list_runs
    try:
        runs = list_runs(limit=max(1, min(int(limit or 30), 200)))
    except Exception as e:
        logger.debug('轨迹列表读取失败: %s', e)
        return []

    out: List[Dict[str, Any]] = []
    for r in runs:
        rid = r.get('run_id')
        if not rid:
            continue
        item = {
            'run_id': rid,
            'mtime': r.get('mtime'),
            'user_message': r.get('user_message') or '',
            'provider': r.get('provider') or '',
            'model': r.get('model') or '',
        }
        try:
            fd = diagnose_run(rid)
        except Exception:
            fd = None
        if fd is None:
            item.update({'status': 'unknown', 'level': 'warn', 'score': None,
                         'primary_fault': None, 'problem_count': 0,
                         'elapsed_ms': None, 'tool_calls': None, 'iterations': None})
        else:
            dg = fd['diagnosis']
            pf = dg.get('primary_fault')
            item.update({
                'status': fd['status'],
                'level': dg['level'],
                'score': dg['score'],
                'problem_count': len(dg.get('problems') or []),
                'primary_fault': ({'code': pf['code'], 'title': pf['title'],
                                   'severity': pf['severity'], 'phase': pf.get('phase')}
                                  if pf else None),
                'elapsed_ms': fd['metrics'].get('elapsed_ms'),
                'tool_calls': fd['metrics'].get('tool_calls'),
                'iterations': fd['metrics'].get('iterations'),
            })
        out.append(item)
    return out



# ---- 诊断结果缓存（2026-09-25）----
# flows/health / problems 在并发压测下 p95 可达数秒：每次全量扫 traces + build_flow。
# 这里加进程内 TTL 缓存：同 limit 在 TTL 内直接复用，失败/空结果不缓存。
_DIAG_CACHE: Dict[str, Any] = {}
_DIAG_CACHE_TS: Dict[str, float] = {}
_DIAG_CACHE_TTL = float(__import__("os").environ.get("AGENT_DIAG_CACHE_TTL", "8"))
_DIAG_CACHE_LOCK = __import__("threading").Lock()


def _diag_cache_get(key: str):
    now = time.time()
    with _DIAG_CACHE_LOCK:
        if key in _DIAG_CACHE and (now - _DIAG_CACHE_TS.get(key, 0)) < _DIAG_CACHE_TTL:
            return _DIAG_CACHE[key]
    return None


def _diag_cache_set(key: str, value: Any) -> None:
    if not isinstance(value, dict) or not value.get("runs"):
        return  # 空结果不缓存，避免污染期粘住
    with _DIAG_CACHE_LOCK:
        _DIAG_CACHE[key] = value
        _DIAG_CACHE_TS[key] = time.time()
        # 简单容量控制
        if len(_DIAG_CACHE) > 32:
            for k in sorted(_DIAG_CACHE_TS, key=_DIAG_CACHE_TS.get)[:16]:
                _DIAG_CACHE.pop(k, None)
                _DIAG_CACHE_TS.pop(k, None)


def invalidate_diag_cache() -> None:
    with _DIAG_CACHE_LOCK:
        _DIAG_CACHE.clear()
        _DIAG_CACHE_TS.clear()


def aggregate_health(limit: int = 100) -> Dict[str, Any]:
    """跨运行的聚合视角：各故障码 / 各阶段出现频率排行 → "哪一部分最常出问题"。

    2026-09-25：TTL 缓存，缓解 /flows/health 并发下 p95 过高。
    """
    _ck = f"health:{max(1, min(int(limit or 100), 200))}"
    _hit = _diag_cache_get(_ck)
    if _hit is not None:
        out = dict(_hit)
        out["cached"] = True
        return out
    from trace_store import list_runs
    try:
        runs = list_runs(limit=max(1, min(int(limit or 100), 200)))
    except Exception as e:
        logger.debug('聚合读取失败: %s', e)
        return {'runs': 0, 'by_status': {}, 'codes': [], 'phases': [], 'avg_elapsed_ms': None}

    n = 0
    by_status: Dict[str, int] = {}
    code_stat: Dict[str, Dict[str, Any]] = {}
    phase_stat: Dict[str, int] = {}
    elapsed_sum = 0
    elapsed_n = 0

    for r in runs:
        rid = r.get('run_id')
        if not rid:
            continue
        try:
            flow = build_flow(rid)
        except Exception:
            flow = None
        if flow is None:
            continue
        n += 1
        st = flow['status']
        by_status[st] = by_status.get(st, 0) + 1
        em = flow['metrics'].get('elapsed_ms')
        if isinstance(em, (int, float)) and em > 0:
            elapsed_sum += em
            elapsed_n += 1
        for p in flow['diagnosis'].get('problems') or []:
            c = code_stat.setdefault(p['code'], {
                'code': p['code'], 'title': p['title'], 'severity': p['severity'],
                'count': 0, 'last_run_id': rid, 'last_at': r.get('mtime'),
            })
            c['count'] += 1
            # 保留更严重的一条作为代表
            if _SEVERITY_ORDER.get(p['severity'], 9) < _SEVERITY_ORDER.get(c['severity'], 9):
                c['title'] = p['title']
                c['severity'] = p['severity']
            if p.get('phase'):
                phase_stat[p['phase']] = phase_stat.get(p['phase'], 0) + 1

    codes = sorted(code_stat.values(), key=lambda c: (-c['count'], _SEVERITY_ORDER.get(c['severity'], 9)))
    for c in codes:
        c['rate'] = round(c['count'] / n, 4) if n else 0.0

    phases = []
    for key, label in PHASES:
        c = phase_stat.get(key, 0)
        if c:
            phases.append({'key': key, 'label': label, 'problem_count': c,
                           'rate': round(c / n, 4) if n else 0.0})
    phases.sort(key=lambda p: -p['problem_count'])

    _payload = {
        'runs': n,
        'by_status': by_status,
        'healthy_rate': round(by_status.get('ok', 0) / n, 4) if n else 0.0,
        'avg_elapsed_ms': int(elapsed_sum / elapsed_n) if elapsed_n else None,
        'codes': codes,
        'phases': phases,
    }
    _diag_cache_set(_ck, _payload)
    return _payload


# ----------------------------------------------------------------------
# 问题页专用聚合：时间统计 + 问题台账 + 优化方向
# ----------------------------------------------------------------------
# 优化方向知识库：code → (优先级权重, 方向标题, 可执行建议列表)
# 权重用于排序：critical=5, warning=3, info=1 再乘以出现率；时间类问题单独加权。
_OPT_DIRECTION: Dict[str, Tuple[int, str, List[str]]] = {
    'PIPELINE_STAGE_FAILED': (
        10, '多Agent阶段失败（限流/凭据/模型）',
        ['确认各阶段 provider 有可用 Key（当前仅 sensenova 时勿选 zhipu）',
         '开启「自动选择大模型」并检查钉扎模型是否仍在目录中',
         '清理前端 localStorage 旧管线配置（PIPE_CFG_VERSION 变更后需重选）',
         '单阶段反复失败时，临时改用全局同一模型验证链路']),
    'GRAPH_INVOKE_RAISED': (
        9, 'LangGraph 图执行抛错',
        ['在流程追踪页查看 graph.error 的 error_type/error_detail',
         '检查 LangGraph 依赖是否完整（便携解释器需 langgraph）',
         '对照 checkpoint 序列化约束：cfg 不可含函数/自定义对象',
         '复现同一 user_message，看是模型/工具还是图状态问题']),
    'RUN_FAILED': (
        8, '整轮运行失败',
        ['先看 primary_fault / run_end.summary.error',
         '确认 LLM 凭据与模型可调用（勿只看 /v1/models 列表）',
         '若伴随 401/403，检查 secure_config 凭据库 backend_ok']),
    'EMPTY_OUTPUT': (
        9, '终稿为空 / 用户看不到回复',
        ['确认 llm_client reasoning 兜底未被移除',
         '提高终稿 max_tokens；reasoning 系模型终稿设 reasoning_effort=none',
         '检查是否零工具+零正文（已改为 ok=false + 可读兜底文案）']),
    'FINALIZE_FAILED': (
        8, '终稿合成失败',
        ['检查 finalize 阶段是否被限流（429）',
         '压缩系统提示/工具结果后再试',
         '看 finalize_failed 事件中的 error 字段']),
    'PLANNER_ERROR': (
        6, '规划失败（计划降级为空）',
        ['确认 _lg_planner 消息以 user 轮结束（智谱 1214）',
         '规划失败通常不致命，但复杂任务会失去分步引导',
         '必要时对简单任务 no_plan=True 省一轮 LLM']),
    'TOOL_ERROR': (
        6, '工具执行失败',
        ['在流程页看失败工具名与 error',
         '确认数据文件存在、列名匹配',
         '金融类工具检查数据源/token；联网工具检查网络'],
         ),
    'NO_TOOL_CALLS': (
        7, '有数据却未调用工具',
        ['确认意图路由未把分析问题误判为 chat',
         '检查前端是否把 file 传给 /api/assistant/chat',
         '模型原生 tools 采纳率低时，依赖文本协议与首轮重试提示']),
    'NO_TOOL_CALLS_NOFILE': (
        2, '无文件零工具（多为正常闲聊）',
        ['若本应分析文件，检查前端 file 参数',
         '行业/标的类问题应走联网/金融工具，而非空答']),
    'NATIVE_FALLBACK': (
        4, '原生 function calling 未被采纳',
        ['对该 provider/model 可设 AGENT_NATIVE_AUTO_DEGRADE=1 自动降级',
         '或直接用文本 JSON 工具协议，省 schema token',
         '检查 tools 下发格式是否被网关剥离']),
    'NATIVE_DEGRADED': (
        3, '原生工具自动降级（自适应保护）',
        ['属保护行为，非故障',
         '需强制恢复时设 AGENT_NATIVE_AUTO_DEGRADE=0 或换模型']),
    'FACT_UNVERIFIED': (
        5, '报告数字缺少可核查来源',
        ['优先让 agent 先 web_search/finance_data 再写数',
         '核对事实审计 ratio=未核验率（>0.5 才报警）',
         '无来源数字应在报告中标注「待补充」']),
    'TRUNCATED': (
        5, '输出被截断',
        ['提高终稿 max_tokens；已支持 finish_reason=length 自动续写',
         '压缩提示与工具结果，减少正文被挤占']),
    'FORCE_FINAL': (
        2, '达到最大轮数自动收尾',
        ['复杂研究任务可提高 max_iterations',
         '若结论覆盖不足，拆成多轮追问']),
    'TOOL_PARAM_DROP': (
        3, '工具参数被丢弃',
        ['对照 _TOOL_PARAM_SCHEMA 补齐参数声明',
         '在提示中强调可用参数名与类型']),
    'KB_MISS': (
        4, '知识库零命中',
        ['检查知识库是否录入对应专题',
         '便携环境无 sentence_transformers 时向量路会降级，依赖 BM25/TF-IDF']),
    'SLOW_STAGE': (
        6, '阶段耗时过高',
        ['看时间统计中哪一阶段 p95 最高',
         '管线阶段可用 PIPELINE_STAGE_FAST=1 裁剪工具/提示',
         '联网工具串行是主因，可开同轮工具并发']),
    'SLOW_LLM_CALL': (
        6, '单次 LLM 调用过慢',
        ['检查是否 reasoning 系模型思考过长',
         '终稿可关思考；调用间隔受自适应节流影响',
         '换更快的 model（如 flash 档）做中间轮次']),
    'RUN_INTERRUPTED': (
        7, '运行未收尾（中断）',
        ['服务重启/进程被杀会留下悬挂 run',
         '检查 begin_run 后是否异常退出；测试轨迹勿写生产目录']),
}


def _percentile(sorted_vals: List[float], p: float) -> Optional[float]:
    """简单百分位（nearest-rank）。vals 需已排序；空返回 None。"""
    if not sorted_vals:
        return None
    if p <= 0:
        return float(sorted_vals[0])
    if p >= 100:
        return float(sorted_vals[-1])
    k = max(0, min(len(sorted_vals) - 1, int(round((p / 100) * len(sorted_vals) + 0.5)) - 1))
    return float(sorted_vals[k])


def problems_dashboard(limit: int = 100) -> Dict[str, Any]:
    """Agent 问题页数据源：时间统计 + 问题台账 + 优化方向。

    2026-09-25：TTL 缓存 + 限制 sample 扫描成本。
    """
    _ck = f"problems:{max(1, min(int(limit or 100), 200))}"
    _hit = _diag_cache_get(_ck)
    if _hit is not None:
        out = dict(_hit)
        out["cached"] = True
        return out

    from trace_store import list_runs
    empty = {
        'runs': 0,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'time': {'avg_ms': None, 'p50_ms': None, 'p90_ms': None, 'p95_ms': None,
                 'max_ms': None, 'min_ms': None, 'samples': 0,
                 'by_phase': [], 'by_day': []},
        'problems': [],
        'directions': [],
        'by_status': {},
        'healthy_rate': 0.0,
        'rule_catalog': [],
        'recent_bad': [],
    }
    try:
        runs = list_runs(limit=max(1, min(int(limit or 100), 200)))
    except Exception as e:
        logger.debug('问题页聚合读取失败: %s', e)
        return empty

    n = 0
    by_status: Dict[str, int] = {}
    elapsed_list: List[float] = []
    phase_ms: Dict[str, List[float]] = {}
    phase_label = {k: v for k, v in PHASES}
    code_stat: Dict[str, Dict[str, Any]] = {}
    day_stat: Dict[str, Dict[str, Any]] = {}
    recent_bad: List[Dict[str, Any]] = []
    # 无问题运行的耗时，用于对比「出问题是否更慢」
    clean_elapsed: List[float] = []

    for r in runs:
        rid = r.get('run_id')
        if not rid:
            continue
        try:
            flow = build_flow(rid)
        except Exception:
            flow = None
        if flow is None:
            continue
        n += 1
        st = flow['status']
        by_status[st] = by_status.get(st, 0) + 1
        m = flow['metrics'] or {}
        em = m.get('elapsed_ms')
        if isinstance(em, (int, float)) and em > 0:
            elapsed_list.append(float(em))
        mtime = r.get('mtime')
        day = ''
        if mtime:
            try:
                day = datetime.fromtimestamp(float(mtime)).strftime('%Y-%m-%d')
            except Exception:
                day = ''
        if day:
            d = day_stat.setdefault(day, {'day': day, 'runs': 0, 'problems': 0,
                                          'elapsed_sum': 0, 'elapsed_n': 0})
            d['runs'] += 1
            if isinstance(em, (int, float)) and em > 0:
                d['elapsed_sum'] += float(em)
                d['elapsed_n'] += 1

        probs = flow['diagnosis'].get('problems') or []
        if not probs and isinstance(em, (int, float)):
            clean_elapsed.append(float(em))

        for p in flow['phases'] or []:
            pk = p.get('key') or 'other'
            pel = p.get('elapsed_ms')
            if isinstance(pel, (int, float)) and pel >= 0:
                phase_ms.setdefault(pk, []).append(float(pel))

        for p in probs:
            code = p.get('code') or 'UNKNOWN'
            c = code_stat.setdefault(code, {
                'code': code,
                'title': p.get('title') or code,
                'severity': p.get('severity') or 'info',
                'phase': p.get('phase'),
                'count': 0,
                'first_at': mtime,
                'last_at': mtime,
                'first_run_id': rid,
                'last_run_id': rid,
                'elapsed_sum': 0.0,
                'elapsed_n': 0,
                'samples': [],
                'hints': [],
            })
            c['count'] += 1
            if _SEVERITY_ORDER.get(p.get('severity'), 9) < _SEVERITY_ORDER.get(c['severity'], 9):
                c['severity'] = p.get('severity')
                c['title'] = p.get('title') or c['title']
            if p.get('phase'):
                c['phase'] = p.get('phase')
            if mtime:
                if c['first_at'] is None or mtime < c['first_at']:
                    c['first_at'] = mtime
                    c['first_run_id'] = rid
                if c['last_at'] is None or mtime > c['last_at']:
                    c['last_at'] = mtime
                    c['last_run_id'] = rid
            if isinstance(em, (int, float)) and em > 0:
                c['elapsed_sum'] += float(em)
                c['elapsed_n'] += 1
            if len(c['samples']) < 3:
                c['samples'].append({
                    'run_id': rid,
                    'mtime': mtime,
                    'user_message': (m.get('user_message') or '')[:120],
                    'detail': (p.get('detail') or '')[:200],
                    'elapsed_ms': em,
                })
            if p.get('hint') and p['hint'] not in c['hints'] and len(c['hints']) < 2:
                c['hints'].append(str(p['hint'])[:200])
            if day:
                day_stat[day]['problems'] += 1

        if probs and len(recent_bad) < 20:
            pf = probs[0]
            recent_bad.append({
                'run_id': rid,
                'mtime': mtime,
                'status': st,
                'elapsed_ms': em,
                'user_message': (m.get('user_message') or '')[:120],
                'model': m.get('model') or '',
                'code': pf.get('code'),
                'title': pf.get('title'),
                'severity': pf.get('severity'),
                'detail': (pf.get('detail') or '')[:180],
                'hint': (pf.get('hint') or '')[:160],
                'problem_count': len(probs),
            })

    # 耗时分位
    elapsed_sorted = sorted(elapsed_list)
    avg_ms = int(sum(elapsed_list) / len(elapsed_list)) if elapsed_list else None
    time_block = {
        'avg_ms': avg_ms,
        'p50_ms': int(_percentile(elapsed_sorted, 50)) if elapsed_sorted else None,
        'p90_ms': int(_percentile(elapsed_sorted, 90)) if elapsed_sorted else None,
        'p95_ms': int(_percentile(elapsed_sorted, 95)) if elapsed_sorted else None,
        'max_ms': int(elapsed_sorted[-1]) if elapsed_sorted else None,
        'min_ms': int(elapsed_sorted[0]) if elapsed_sorted else None,
        'samples': len(elapsed_list),
        'clean_avg_ms': int(sum(clean_elapsed) / len(clean_elapsed)) if clean_elapsed else None,
        'clean_samples': len(clean_elapsed),
    }

    by_phase = []
    for key, label in PHASES:
        vals = sorted(phase_ms.get(key) or [])
        if not vals:
            continue
        by_phase.append({
            'key': key,
            'label': label,
            'count': len(vals),
            'avg_ms': int(sum(vals) / len(vals)),
            'p50_ms': int(_percentile(vals, 50) or 0),
            'p95_ms': int(_percentile(vals, 95) or 0),
            'max_ms': int(vals[-1]),
        })
    by_phase.sort(key=lambda x: -(x['p95_ms'] or 0))
    time_block['by_phase'] = by_phase

    by_day = []
    for day in sorted(day_stat.keys()):
        d = day_stat[day]
        by_day.append({
            'day': day,
            'runs': d['runs'],
            'problems': d['problems'],
            'avg_ms': int(d['elapsed_sum'] / d['elapsed_n']) if d['elapsed_n'] else None,
        })
    time_block['by_day'] = by_day[-14:]  # 最近 14 天

    # 问题台账
    problems_out = []
    for c in code_stat.values():
        sev = c['severity']
        rate = round(c['count'] / n, 4) if n else 0.0
        avg_when = int(c['elapsed_sum'] / c['elapsed_n']) if c['elapsed_n'] else None
        weight = _SEVERITY_PENALTY.get(sev, 5)
        impact = round(weight * max(rate, 0.01) * 100, 2)
        problems_out.append({
            'code': c['code'],
            'title': c['title'],
            'severity': sev,
            'phase': c['phase'] or phase_label.get(c['phase'] or '', c['phase']),
            'phase_key': c['phase'],
            'count': c['count'],
            'rate': rate,
            'first_at': c['first_at'],
            'last_at': c['last_at'],
            'first_run_id': c['first_run_id'],
            'last_run_id': c['last_run_id'],
            'avg_elapsed_ms': avg_when,
            'samples': c['samples'],
            'hints': c['hints'],
            'impact': impact,
        })
    problems_out.sort(key=lambda x: (-x['impact'], -x['count'],
                                     _SEVERITY_ORDER.get(x['severity'], 9)))

    # 优化方向：按 impact 排序，结合知识库与运行时特征
    directions = []
    seen_dir = set()
    for p in problems_out:
        meta = _OPT_DIRECTION.get(p['code'])
        if not meta:
            # 未知码：用诊断 hint 兜底
            title = f"处理 {p['title']}"
            tips = list(p.get('hints') or []) or ['打开流程追踪页查看该 run 的时间线与证据']
            prio = max(1, int(p['impact']))
        else:
            prio, title, tips = meta
            # 出现率加权
            prio = prio + int(p['rate'] * 10)
        if title in seen_dir:
            continue
        seen_dir.add(title)
        directions.append({
            'code': p['code'],
            'priority': prio,
            'title': title,
            'severity': p['severity'],
            'count': p['count'],
            'rate': p['rate'],
            'avg_elapsed_ms': p['avg_elapsed_ms'],
            'actions': tips,
            'phase': p.get('phase'),
        })

    # 时间型附加方向：某阶段 p95 过高
    for ph in by_phase[:3]:
        if (ph.get('p95_ms') or 0) >= SLOW_STAGE_MS:
            key = f"SLOW_PHASE::{ph['key']}"
            if key not in seen_dir:
                seen_dir.add(key)
                directions.append({
                    'code': key,
                    'priority': 6 + (2 if (ph['p95_ms'] or 0) >= SLOW_STAGE_MS * 2 else 0),
                    'title': f"阶段「{ph['label']}」p95 过高",
                    'severity': 'info',
                    'count': ph['count'],
                    'rate': round(ph['count'] / n, 4) if n else 0,
                    'avg_elapsed_ms': ph['avg_ms'],
                    'phase': ph['key'],
                    'actions': [
                        f"该阶段 p95={ph['p95_ms']}ms / max={ph['max_ms']}ms，优先优化此处",
                        '管线阶段：工具白名单 + slim_prompt + 同轮工具并发',
                        '联网类工具串行执行时延叠加，检查是否可并行或缓存',
                        'LLM 过慢：中间轮次换 flash 档，终稿关 reasoning'],
                })

    if time_block.get('avg_ms') and time_block.get('p95_ms') and \
            time_block['p95_ms'] > max(20000, (time_block['avg_ms'] or 0) * 3):
        directions.append({
            'code': 'TIME_SKEW',
            'priority': 5,
            'title': '耗时长尾明显（p95 ≫ 均值）',
            'severity': 'info',
            'count': 0,
            'rate': 0,
            'avg_elapsed_ms': time_block['p95_ms'],
            'phase': None,
            'actions': [
                f"均值 {time_block['avg_ms']}ms 但 p95 {time_block['p95_ms']}ms，存在少量极慢请求",
                '对照 by_phase 找长尾阶段；检查工具超时与 LLM 重试',
                '避免把全部 32 个工具 schema 每轮重发'],
        })

    directions.sort(key=lambda d: (-d['priority'], -d.get('count', 0)))

    try:
        catalog = rule_catalog()
    except Exception:
        catalog = []

    # 周环比：最近 7 天 vs 前 7 天（按 mtime 日切）
    def _bucket_day(ts):
        if not ts:
            return ''
        try:
            return datetime.fromtimestamp(float(ts)).strftime('%Y-%m-%d')
        except Exception:
            return ''

    today = datetime.now().date()
    this_week = {'runs': 0, 'problems': 0, 'elapsed_sum': 0.0, 'elapsed_n': 0}
    last_week = {'runs': 0, 'problems': 0, 'elapsed_sum': 0.0, 'elapsed_n': 0}
    for d in by_day:
        try:
            day = datetime.strptime(d['day'], '%Y-%m-%d').date()
        except Exception as e:
            logger.debug('日期格式非 %Y-%m-%d，跳过该日: %s', e)
            continue
        delta = (today - day).days
        bucket = this_week if 0 <= delta <= 6 else (last_week if 7 <= delta <= 13 else None)
        if bucket is None:
            continue
        bucket['runs'] += d.get('runs') or 0
        bucket['problems'] += d.get('problems') or 0
        if d.get('avg_ms') is not None:
            bucket['elapsed_sum'] += float(d['avg_ms']) * max(1, d.get('runs') or 1)
            bucket['elapsed_n'] += max(1, d.get('runs') or 1)

    def _week_view(w):
        return {
            'runs': w['runs'],
            'problems': w['problems'],
            'avg_ms': int(w['elapsed_sum'] / w['elapsed_n']) if w['elapsed_n'] else None,
        }

    wow = {
        'this_week': _week_view(this_week),
        'last_week': _week_view(last_week),
        'delta': {
            'runs': this_week['runs'] - last_week['runs'],
            'problems': this_week['problems'] - last_week['problems'],
            'avg_ms': (int(this_week['elapsed_sum'] / this_week['elapsed_n']) -
                       int(last_week['elapsed_sum'] / last_week['elapsed_n']))
            if this_week['elapsed_n'] and last_week['elapsed_n'] else None,
        },
    }

    _payload = {
        'runs': n,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'by_status': by_status,
        'healthy_rate': round(by_status.get('ok', 0) / n, 4) if n else 0.0,
        'time': time_block,
        'problems': problems_out,
        'directions': directions[:12],
        'recent_bad': recent_bad,
        'rule_catalog': catalog,
        'week_over_week': wow,
    }
    _diag_cache_set(_ck, _payload)
    return _payload
