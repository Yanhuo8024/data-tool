#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
data.tool AI 助手运行轨迹适配器（零第三方依赖）
=================================================

把 data.tool 的 AI 助手轨迹，翻译成可视化器（static/agent-flow-viz.html）能直接消费的
「Spec（拓扑）」与「状态快照（agents / links / summary / logs）」。

**取数方式（2026-09-18 起）**：跑在 data.tool 进程内，轨迹**直读 trace_store**，
不再绕 HTTP 请求自己的 127.0.0.1:5000。对外只经过
`routes/ai_assistant_routes.py` 的 `/api/assistant/flowviz/*` 暴露给前端。

若沿用 HTTP 视角，等价于：
    GET /api/assistant/traces                    运行清单（→ list_runs）
    GET /api/assistant/traces/<run_id>           单次运行的完整事件流（→ fetch_trace）

上游事件词表（13 类）：
    run_start               model / provider / skill / user_message
    agent_config            max_iterations / plan / resume / skill
    llm_call                iteration / content_len / usage / native / has_tool_calls / duration_ms
    tool_result             tool / iteration / status / duration_ms / cached / params / preview
    finalize.start          round / messages / max_iterations / truncated
    finalize.done           content_len / duration_ms / ok / truncated
    pipeline.stage_start    index / stage / model / provider / iters
    pipeline.stage_done     index / stage / ok / elapsed_s / chars / error / model / provider
    pipeline.stage_error    index / stage / ok / elapsed_s / chars / error / model / provider
    fallback_retry          mode
    native_tools_degraded   misses / model
    fact_audit              ratio / total / unverified
    run_end                 ok / outcome / summary（summary 内含 elapsed_ms / errors / failed_stages）

上游有两种运行形态，适配器都会认：
    形态 A「单助手推理循环」  run_start → agent_config → llm_call/tool_result×N
                              → finalize.start/done → run_end
    形态 B「多阶段流水线」    run_start → pipeline.stage_start/done × N → run_end
                              （每阶段自带 elapsed_s，即该步的精确耗时）

逐步计时（timeline）的取值优先级：
    1. 事件自带耗时 —— pipeline 的 elapsed_s、finalize/llm/tool 的 duration_ms  → src='exact'
    2. 相邻事件时间戳之差（ts 有小数秒）                                    → src='gap'
    3. 都没有                                                              → src='none'

映射出的图谱形如：
    用户提问 → AI 助手 → [降级回退] → 推理 #1 → [工具…] → 推理 #2 → 事实核查 → 结果交付

单独运行可直接查看效果（需在 data.tool 根目录下执行，以便 import trace_store）：
    python flowviz_adapter.py                       # 适配最新一次运行
    python flowviz_adapter.py --run <run_id>
    python flowviz_adapter.py --list                # 列出最近运行
"""
from __future__ import print_function

import hashlib
import json
import re
import threading
import time

# 注意：这里**不再 import urllib**。适配器已从「HTTP 拉自己」改为
# 「进程内直读 trace_store」，既少一次网络往返，也不受端口/限流影响。
DEFAULT_BASE = ''        # 兼容旧签名保留，本地模式下无意义
AGENT_VERSION = '1.2.0'  # 1.1.0 → 1.2.0：本地直读 + 事实溯源/终稿语义对齐上游
TIMEOUT = 10

# ---------------------------------------------------------------- 事件词表
T_START = 'run_start'
T_CONFIG = 'agent_config'
T_LLM = 'llm_call'
T_TOOL = 'tool_result'
T_FALLBACK = 'fallback_retry'
T_DEGRADED = 'native_tools_degraded'
T_AUDIT = 'fact_audit'
T_END = 'run_end'
T_FIN_START = 'finalize.start'
T_FIN_DONE = 'finalize.done'
T_P_START = 'pipeline.stage_start'
T_P_DONE = 'pipeline.stage_done'
T_P_ERR = 'pipeline.stage_error'

# 阶段名里出现这些词 → 该阶段按「审核者」渲染（菱形），其余按「执行者」
REVIEW_HINT = re.compile(u'风控|合规|审核|校验|质检|核查|审计|评审|复核|review|audit|compliance|check',
                         re.I)
# 这些阶段名 → 按「交付物」渲染（胶囊）
OUTPUT_HINT = re.compile(u'终稿|汇总|合成|交付|final|output|summary', re.I)

# 并行工作组：连续命中这些词的阶段合并到同一列同时呈现（扇出 → 并行 → 扇入）
PARALLEL_HINT = re.compile(u'市场分析师|量化建模师|风控合规官')


def _stage_blocks(stages):
    """把连续命中 PARALLEL_HINT 的阶段合成并行组（>=2 个才合并）。"""
    blocks, buf = [], []
    for rec in stages:
        if PARALLEL_HINT.search(rec.get('name') or ''):
            buf.append(rec)
            continue
        if buf:
            blocks.append({'members': buf, 'merged': len(buf) >= 2})
            buf = []
        blocks.append({'members': [rec], 'merged': False})
    if buf:
        blocks.append({'members': buf, 'merged': len(buf) >= 2})
    return blocks

# 工具调用状态 → 可视化器状态词
OK_WORDS = ('ok', 'success', 'succeeded', 'done', 'completed', 'finish', 'finished', 'pass')
BAD_WORDS = ('error', 'fail', 'failed', 'exception', 'timeout', 'denied', 'reject', 'rejected')


# ---------------------------------------------------------------- 本地取数
#
# 生产形态：本模块跑在 data.tool 进程内（由 routes/ai_assistant_routes.py 调用），
# 轨迹直接读 trace_store，不再绕 HTTP 去请求自己的 127.0.0.1:5000
# —— 省一次网络往返，也不受接口限流影响，端口怎么配都不影响。
# base 参数为兼容既有调用签名而保留，本地模式下忽略。
class TTLCache(object):
    """带 TTL 的小缓存。轮询频率约 1 次/秒，缓存既不拖慢实时性又能扛住高频读盘。"""

    def __init__(self, ttl, cap=64):
        self.ttl = ttl
        self.cap = cap
        self._lock = threading.Lock()
        self._d = {}

    def get(self, key, producer):
        now = time.time()
        with self._lock:
            hit = self._d.get(key)
            if hit and now - hit[0] < self.ttl:
                return hit[1]
        val = producer()
        with self._lock:
            self._d[key] = (time.time(), val)
            if len(self._d) > self.cap:
                for k in list(self._d.keys())[:self.cap // 4]:
                    self._d.pop(k, None)
        return val

    def peek(self, key):
        """取最后一次成功值（无论是否过期）——读取失败时降级兜底用。"""
        with self._lock:
            hit = self._d.get(key)
            return hit[1] if hit else None

    def drop(self, key):
        with self._lock:
            self._d.pop(key, None)


_RUNS_CACHE = TTLCache(3.0)      # 清单变化低频
_TRACE_CACHE = TTLCache(1.0)     # 明细要跟得上"正在跑"的运行，缓一秒即可

_SERVED = {}                        # {cid: rev}  —— 记录某个浏览器已拿到哪一版拓扑
_SERVED_LOCK = threading.Lock()


class UpstreamLimited(Exception):
    """历史上用于上游 429。本地直读模式下不会再抛，保留类型以兼容调用方。"""

    def __init__(self, retry_after=0):
        Exception.__init__(self, 'upstream rate limited')
        self.retry_after = int(retry_after or 0)


class UpstreamMissing(Exception):
    """清单里列着某次运行，但明细读不到（文件正在写入 / 已被清理）。"""

    def __init__(self, msg, tried=None):
        Exception.__init__(self, msg)
        self.tried = list(tried or [])


def fetch_json(base=None, path='', timeout=TIMEOUT):
    """本地模式下不再发 HTTP。保留仅为兼容旧调用点，被调用即报错提示。"""
    raise RuntimeError('flowviz_adapter 已切换为本地直读模式（无需 HTTP 取数）')


def _load_trace_dict(run_id):
    """直读单次运行的轨迹，返回与上游 HTTP 接口同构的字典；不存在返回 None。"""
    from trace_store import read_run
    run = read_run(run_id)
    if run is None:
        return None
    return {'ok': True,
            'run': run.get('run_id') or run_id,
            'count': run.get('count') or len(run.get('events') or []),
            'file': run.get('file') or '',
            'events': run.get('events') or []}


def list_runs(base=None, limit=20):
    """运行清单（新的在前），直接读本地轨迹目录。"""
    def _produce():
        from trace_store import list_runs as _ts_list
        return _ts_list(limit=200) or []

    try:
        runs = _RUNS_CACHE.get('local', _produce)
    except (Exception, SystemExit):
        runs = _RUNS_CACHE.peek('local') or []

    out = []
    for it in (runs or [])[:max(1, int(limit))]:
        if not isinstance(it, dict):
            continue
        out.append({
            'run_id': it.get('run_id') or it.get('run'),
            'model': it.get('model') or '',
            'provider': it.get('provider') or '',
            'ok': it.get('ok'),
            'mtime': it.get('mtime'),
            'user_message': (it.get('user_message') or '')[:200],
            'file': it.get('file') or '',
        })
    return out


def resolve_run(base=None, run=None):
    """把 latest / 空值解析成真实 run_id。"""
    run = (run or '').strip()
    if run and run not in ('latest', 'last', 'newest', '-'):
        return run
    runs = list_runs(None, 1)
    return runs[0]['run_id'] if runs and runs[0].get('run_id') else None


def fetch_trace(base=None, run=None):
    """读取单次运行的完整事件流（本地直读 + 1s 短缓存）。"""
    rid = resolve_run(None, run)
    if not rid:
        raise UpstreamMissing('还没有任何运行记录 —— 先和 AI 助手说一句话')

    def _produce():
        d = _load_trace_dict(rid)
        if d is None:
            raise UpstreamMissing('轨迹不存在或已被清理: %s' % rid, [(rid, 404)])
        return d

    return _TRACE_CACHE.get(rid, _produce)


def fetch_trace_fresh(base=None, run=None):
    rid = resolve_run(None, run)
    if rid:
        _TRACE_CACHE.drop(rid)
    return fetch_trace(None, run)


def fetch_trace_soft(base=None, run=None, max_candidates=6):
    """取一次轨迹；当前 run 不可读时在候选 run 之间退让，别把 404 直接甩给页面。

    刚落盘的 trace 文件可能短时间读到半截，或刚被清理掉。
    此时退到下一个可用的 run，比让整个画布停下来更合理。

    返回 (trace, used_run_id, skipped)；候选全部失败时抛 UpstreamMissing。
    """
    want = (run or '').strip()
    cands, tried = [], []

    try:
        rid = resolve_run(None, want or None)
        if rid:
            cands.append(rid)
    except (Exception, SystemExit) as exc:
        tried.append((want or 'latest', str(exc)[:80]))

    # 清单缓存可能还是上一拍的，重新列一遍拿候选
    _RUNS_CACHE.drop('local')
    try:
        for it in list_runs(None, max(8, max_candidates)):
            rid = it.get('run_id')
            if rid and rid not in cands:
                cands.append(rid)
    except (Exception, SystemExit) as exc:
        tried.append(('list', str(exc)[:80]))

    for rid in cands[:max(1, max_candidates)]:
        try:
            return fetch_trace(None, rid), rid, tried
        except (Exception, SystemExit) as exc:
            tried.append((rid, str(exc)[:80]))

    raise UpstreamMissing('没有可用的运行明细（试过 %d 条）' % len(tried), tried)


# ---------------------------------------------------------------- 工具函数
def _num(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f


def _int(v, default=0):
    f = _num(v)
    return int(f) if f is not None else default


def _clip(s, n=160):
    s = str(s if s is not None else '').replace('\r', ' ').replace('\n', ' ').strip()
    return s if len(s) <= n else s[:n - 1] + '…'


def _norm_status(v):
    s = str(v or '').strip().lower()
    if not s:
        return 'done'
    for w in BAD_WORDS:
        if w in s:
            return 'failed'
    for w in OK_WORDS:
        if w in s:
            return 'done'
    return 'done'


# 事实溯源**未核验率**高于此值 → 提示数字缺出处。
# data.tool 的 fact_audit.ratio = missing/total（未核验率），不是"通过率"。
# 2026-09-18 上游修正语义前，本适配器把 ratio 当通过率用（ratio<0.8 判不通过），
# 方向是反的：70% 未核验反而不报、30% 未核验却误报。阈值口径与 agent_flow 保持一致。
FACT_UNVERIFIED_WARN = 0.5


def _final_flags(d):
    """拆分终稿收尾语义 → (truncated, force_final)。

    上游 2026-09-18 起把两件事分开：
      force_final = 用满迭代预算后强制收尾（正常流程，info）
      truncated   = 终稿真的被 max_tokens 截断（异常，warn）
    旧事件没有 force_final 键，它的 truncated=true 其实是 force_final，
    为兼容历史轨迹按此还原，避免把正常收尾报成"输出截断"。
    """
    d = d or {}
    if 'force_final' in d:
        return bool(d.get('truncated')), bool(d.get('force_final'))
    return False, bool(d.get('truncated'))


def index_events(trace):
    """按类型切开事件流，并抽取派生量。"""
    evs = trace.get('events') or []
    idx = {
        'events': evs,
        'starts': [], 'configs': [], 'llms': [], 'tools': [],
        'fallbacks': [], 'degraded': [], 'audits': [], 'ends': [],
        'p_starts': [], 'p_dones': [], 'p_errs': [],
        'fin_starts': [], 'fin_dones': [],
    }
    bucket = {
        T_START: 'starts', T_CONFIG: 'configs', T_LLM: 'llms', T_TOOL: 'tools',
        T_FALLBACK: 'fallbacks', T_DEGRADED: 'degraded', T_AUDIT: 'audits', T_END: 'ends',
        T_P_START: 'p_starts', T_P_DONE: 'p_dones', T_P_ERR: 'p_errs',
        T_FIN_START: 'fin_starts', T_FIN_DONE: 'fin_dones',
    }
    for e in evs:
        if not isinstance(e, dict):
            continue
        b = bucket.get(e.get('type'))
        if b:
            idx[b].append(e)

    def data_of(lst):
        return [(e, (e.get('data') or {})) for e in lst]

    idx['start_pairs'] = data_of(idx['starts'])
    idx['config_pairs'] = data_of(idx['configs'])
    idx['llm_pairs'] = data_of(idx['llms'])
    idx['tool_pairs'] = data_of(idx['tools'])
    idx['fallback_pairs'] = data_of(idx['fallbacks'])
    idx['degraded_pairs'] = data_of(idx['degraded'])
    idx['audit_pairs'] = data_of(idx['audits'])
    idx['end_pairs'] = data_of(idx['ends'])
    idx['p_start_pairs'] = data_of(idx['p_starts'])
    idx['p_done_pairs'] = data_of(idx['p_dones'])
    idx['p_err_pairs'] = data_of(idx['p_errs'])
    idx['fin_start_pairs'] = data_of(idx['fin_starts'])
    idx['fin_done_pairs'] = data_of(idx['fin_dones'])

    # 迭代序号：llm_call 与 tool_result 的 iteration
    iters = set()
    for _, d in idx['llm_pairs'] + idx['tool_pairs']:
        iters.add(_int(d.get('iteration'), 1) or 1)
    idx['iters'] = sorted(iters)

    # 每轮工具调用
    by_iter = {}
    for _, d in idx['tool_pairs']:
        i = _int(d.get('iteration'), 1) or 1
        by_iter.setdefault(i, []).append(d)
    idx['tools_by_iter'] = by_iter

    # token 汇总
    tok = 0
    for _, d in idx['llm_pairs']:
        u = d.get('usage') or {}
        tok += _int(u.get('total_tokens'))
    idx['tokens'] = tok

    # 耗时
    t0 = t1 = None
    for e in evs:
        ts = e.get('ts')
        if not ts:
            continue
        if t0 is None:
            t0 = ts
        t1 = ts
    idx['first_ts'] = t0
    idx['last_ts'] = t1
    idx['elapsed'] = _ts_diff(t0, t1)

    # 每条事件的绝对时间戳（秒）：用于算每一步的起点偏移与相邻间隔
    tss = [_ts_abs(e.get('ts')) for e in evs]
    idx['ts_abs'] = tss
    idx['ts_base'] = next((v for v in tss if v is not None), None)

    # run_end 汇总
    end_d = idx['end_pairs'][0][1] if idx['end_pairs'] else {}
    summ = end_d.get('summary') or {}
    if summ:
        ms = _num(summ.get('elapsed_ms'))
        if ms is not None:
            idx['elapsed'] = ms / 1000.0
        st = _num(summ.get('total_tokens'))
        if st is not None and st > 0:
            idx['tokens'] = int(st)
    idx['end_data'] = end_d
    idx['done'] = bool(idx['ends'])
    idx['ok'] = bool(end_d.get('ok')) if idx['ends'] else None

    idx['run_id'] = trace.get('run')
    idx['model'] = (idx['start_pairs'][0][1].get('model') if idx['start_pairs'] else '') or ''
    idx['provider'] = (idx['start_pairs'][0][1].get('provider') if idx['start_pairs'] else '') or ''
    idx['user_message'] = (idx['start_pairs'][0][1].get('user_message') if idx['start_pairs'] else '') or ''
    cfg = idx['config_pairs'][0][1] if idx['config_pairs'] else {}
    idx['max_iterations'] = _int(cfg.get('max_iterations'), 0)
    idx['plan'] = bool(cfg.get('plan'))
    idx['resume'] = bool(cfg.get('resume'))
    idx['skill'] = (cfg.get('skill')
                    or (idx['start_pairs'][0][1].get('skill') if idx['start_pairs'] else '')
                    or '')
    idx['stages'] = _collect_stages(idx)
    idx['mode'] = 'pipeline' if idx['p_starts'] else 'loop'
    # run_start 没带模型时（流水线形态下模型是各阶段各自指定的，begin_run 往往拿不到），
    # 回落到首个阶段的模型 —— 否则「AI 助手」节点会显示成"未知模型"。
    if not idx['model']:
        for _rec in idx['stages']:
            if _rec.get('model'):
                idx['model'] = _rec['model']
                idx['provider'] = idx['provider'] or _rec.get('provider') or ''
                break
    idx['stage_consumed'] = set(r['ei'] for r in idx['stages'] if r.get('ei') is not None)
    return idx


def _ts_diff(a, b):
    """两个 ISO 时间字符串的秒差（解析失败返回 None）。"""
    if not a or not b:
        return None
    fmt = '%Y-%m-%dT%H:%M:%S'

    def cut(s):
        s = str(s)
        return s[:19] if len(s) >= 19 else s

    try:
        ta = time.strptime(cut(a), fmt)
        tb = time.strptime(cut(b), fmt)
    except ValueError:
        return None
    # 补上小数秒，提升短运行的精度
    def frac(s):
        s = str(s)
        if '.' in s:
            try:
                return float('0.' + s.split('.', 1)[1][:6])
            except ValueError:
                return 0.0
        return 0.0
    return (time.mktime(tb) + frac(b)) - (time.mktime(ta) + frac(a))


def _ts_abs(s):
    """ISO 时间串 → epoch 秒（含小数秒）；解析失败返回 None。"""
    if not s:
        return None
    s = str(s)
    try:
        base = time.mktime(time.strptime(s[:19], '%Y-%m-%dT%H:%M:%S'))
    except ValueError:
        return None
    frac = 0.0
    if len(s) > 19 and '.' in s[19:]:
        digits = ''.join(ch for ch in s[19:].split('.', 1)[1] if ch.isdigit())[:6]
        if digits:
            frac = float('0.' + digits)
    return base + frac


def _fmt_sec(v, na=u'—'):
    """秒 → 可读文本。小于 10ms 的不谎报 0.00s。"""
    if v is None:
        return na
    if v < 0.005:
        return u'<0.01s'
    if v < 10:
        return u'%.2fs' % v
    if v < 60:
        return u'%.1fs' % v
    m = int(v // 60)
    return u'%dm%.0fs' % (m, v - m * 60)


def _collect_stages(idx):
    """把 pipeline.stage_start 与配对的 stage_done / stage_error 合成阶段记录。"""
    starts = [(i, (e.get('data') or {}))
              for i, e in enumerate(idx['events']) if e.get('type') == T_P_START]
    ends = [(i, (e.get('data') or {}))
            for i, e in enumerate(idx['events']) if e.get('type') in (T_P_DONE, T_P_ERR)]
    out, used = [], set()
    for n, (si, sd) in enumerate(starts):
        k = _int(sd.get('index'), 0)
        hit = None
        for j, (ei, ed) in enumerate(ends):
            if j in used:
                continue
            if k and _int(ed.get('index'), 0) == k:
                hit = (j, ei, ed)
                break
        if hit is None:                       # 按出现顺序兜底配对
            for j, (ei, ed) in enumerate(ends):
                if j not in used and ei > si:
                    hit = (j, ei, ed)
                    break
        rec = {
            'no': k or (n + 1),
            'name': _clip(sd.get('stage') or (u'阶段 %d' % (k or n + 1)), 28),
            'model': sd.get('model') or '', 'provider': sd.get('provider') or '',
            'iters': _int(sd.get('iters'), 0),
            'sec': None, 'chars': None, 'ok': None, 'error': None,
            'state': 'running', 'si': si, 'ei': None,
        }
        if hit is not None:
            j, ei, ed = hit
            used.add(j)
            rec['ei'] = ei
            rec['sec'] = _num(ed.get('elapsed_s'))
            rec['chars'] = _int(ed.get('chars'), 0)
            rec['ok'] = bool(ed.get('ok')) if ed.get('ok') is not None else None
            rec['error'] = ed.get('error')
            rec['model'] = ed.get('model') or rec['model']
            rec['provider'] = ed.get('provider') or rec['provider']
            if ed.get('error') or rec['ok'] is False:
                rec['state'] = 'failed'
                rec['ok'] = False
            else:
                rec['state'] = 'done'
                if rec['ok'] is None:
                    rec['ok'] = True
        out.append(rec)
    return out


def build_timeline(idx):
    """全链路每一步的计时（严格按事件顺序）。

    元素字段：
        id      节点 id（与 Spec / 状态快照同一命名空间，前端据此把时长挂到节点上）
        name    步骤名
        kind    start/agent/stage/iter/tool/finalize/fallback/audit/end
        state   done/failed/running
        t0      距首个事件的偏移（秒，可能为 None）
        dur     该步耗时（秒，可能为 None）
        src     exact（上游自带） / gap（相邻事件间隔推算） / none
        detail  一句话说明
        meta    附加指标（tokens/chars/model/round/total_ms…）
    """
    evs = idx['events']
    tss = idx.get('ts_abs') or []
    base = idx.get('ts_base')

    def gap(i):
        if i + 1 >= len(tss):
            return None
        a, b = tss[i], tss[i + 1]
        if a is None or b is None:
            return None
        d = b - a
        return d if 0 <= d <= 3600 else None

    def off(i):
        if base is None or i >= len(tss) or tss[i] is None:
            return None
        return tss[i] - base

    steps = []
    seat = {}                       # id → steps 中的下标（同 id 合并，如多轮降级）

    def emit(i, nid, name, kind, state=None, ms=None, detail=u'', meta=None, error=None):
        dur, src = None, 'none'
        if ms is not None:
            dur, src = ms / 1000.0, 'exact'
        else:
            g = gap(i)
            if g is not None:
                dur, src = g, 'gap'
        if nid and nid in seat:
            prev = steps[seat[nid]]
            if dur is not None:
                prev['dur'] = round((prev['dur'] or 0) + dur, 3)
                prev['src'] = 'exact' if (prev['src'] == 'exact' or src == 'exact') else src
            prev['meta']['calls'] = _int(prev['meta'].get('calls'), 0) + 1
            if state == 'failed':
                prev['state'] = 'failed'
            if detail:
                prev['detail'] = (prev['detail'] + u' ｜ ' + detail)[:220]
            if error:
                prev['error'] = error
            return prev
        it = {
            'id': nid, 'name': name, 'kind': kind,
            'state': state or 'done',
            't0': (round(off(i), 3) if off(i) is not None else None),
            'dur': (round(dur, 3) if dur is not None else None),
            'src': src, 'detail': _clip(detail, 200),
            'meta': meta or {}, 'error': error,
        }
        steps.append(it)
        if nid:
            seat[nid] = len(steps) - 1
        return it

    tool_seq = {}
    for i, e in enumerate(evs):
        t = e.get('type')
        d = e.get('data') or {}
        if t == T_START:
            emit(i, 'start', u'用户提问', 'start',
                 detail=_clip(d.get('user_message'), 90) or u'（未记录提问内容）',
                 meta={'model': d.get('model') or '', 'skill': d.get('skill') or ''})
        elif t == T_CONFIG:
            emit(i, 'agent', u'AI 助手', 'agent',
                 detail=u'运行配置 · 最多 %d 轮 · plan=%s%s' % (
                     _int(d.get('max_iterations')), 'on' if d.get('plan') else 'off',
                     u' · skill=%s' % d.get('skill') if d.get('skill') else ''),
                 meta={'max_iterations': _int(d.get('max_iterations')),
                       'plan': bool(d.get('plan'))})
        elif t == T_P_START:
            rec = None
            for r in idx['stages']:
                if r['si'] == i:
                    rec = r
                    break
            if rec is None:
                continue
            emit(i, 'stage%d' % rec['no'], rec['name'], 'stage',
                 state=rec['state'], ms=(rec['sec'] * 1000.0 if rec['sec'] is not None else None),
                 detail=u'第 %d 阶段 · %s / %s' % (rec['no'], rec['provider'] or '-', rec['model'] or '-'),
                 meta={'model': rec['model'], 'provider': rec['provider'],
                       'iters': rec['iters'], 'chars': rec['chars'],
                       'index': rec['no']},
                 error=(_clip(rec['error'], 160) if rec['error'] else None))
            if rec['state'] == 'failed':
                steps[seat['stage%d' % rec['no']]]['state'] = 'failed'
            # 该阶段的模型/提供方补进 detail
            if rec['model']:
                it = steps[seat['stage%d' % rec['no']]]
                it['detail'] = _clip(u'%s · %s' % (rec['model'], it['detail']), 200)
        elif t in (T_P_DONE, T_P_ERR):
            if i in idx.get('stage_consumed', ()):
                continue
            k = _int(d.get('index'), 0)
            emit(i, 'stage%d' % k if k else '', (d.get('stage') or u'阶段'), 'stage',
                 state=('failed' if (d.get('error') or d.get('ok') is False) else 'done'),
                 ms=((_num(d.get('elapsed_s')) or 0) * 1000.0),
                 detail=_clip(d.get('error') or u'阶段完成', 120),
                 meta={'index': k, 'chars': _int(d.get('chars'))},
                 error=(_clip(d.get('error'), 160) if d.get('error') else None))
        elif t == T_LLM:
            it_no = _int(d.get('iteration'), 1) or 1
            u = d.get('usage') or {}
            ms = _num(d.get('duration_ms'))
            emit(i, 'iter%d' % it_no, u'推理 #%d' % it_no, 'iter',
                 ms=(ms if (ms is not None and ms > 0) else None),
                 detail=u'返回 %d 字符 · %d tokens%s' % (
                     _int(d.get('content_len')), _int(u.get('total_tokens')),
                     u' · 请求工具' if d.get('has_tool_calls') else ''),
                 meta={'tokens': _int(u.get('total_tokens')),
                       'chars': _int(d.get('content_len')),
                       'native': d.get('native')})
        elif t == T_TOOL:
            it_no = _int(d.get('iteration'), 1) or 1
            tool_seq[it_no] = tool_seq.get(it_no, 0) + 1
            k = tool_seq[it_no]
            st = _norm_status(d.get('status'))
            ms = _num(d.get('duration_ms'))
            emit(i, 'tool%d_%d' % (it_no, k), _clip(d.get('tool') or u'工具', 24), 'tool',
                 state=st, ms=ms,
                 detail=u'%s · %s%s' % (_clip(d.get('status'), 16),
                                        _clip(d.get('preview'), 80) or u'无预览',
                                        u' · 命中缓存' if d.get('cached') else ''),
                 meta={'tokens': 0, 'iteration': it_no, 'cached': bool(d.get('cached'))},
                 error=(_clip(d.get('preview'), 160)
                        if st == 'failed' else None))
        elif t in (T_FALLBACK, T_DEGRADED):
            txt = (u'兜底重试 · mode=%s' % _clip(d.get('mode'), 24)) if t == T_FALLBACK                 else (u'原生工具调用降级 · 未识别 %d 次' % _int(d.get('misses')))
            emit(i, 'fallback', u'降级回退', 'fallback', detail=txt,
                 meta={'mode': d.get('mode') or '', 'misses': _int(d.get('misses'))})
        elif t == T_FIN_DONE:
            ms = _num(d.get('duration_ms'))
            _trunc, _forced = _final_flags(d)
            emit(i, 'finalize', u'终稿合成', 'finalize',
                 state=('done' if d.get('ok') in (None, True) else 'failed'),
                 ms=ms,
                 detail=u'%d 字符%s%s' % (_int(d.get('content_len')),
                                         u' · 输出被截断' if _trunc
                                         else (u' · 达轮数上限收尾' if _forced else ''),
                                         u' · 用时 %s' % _fmt_sec((ms or 0) / 1000.0)),
                 meta={'chars': _int(d.get('content_len')),
                       'truncated': _trunc, 'force_final': _forced},
                 error=(u'终稿未成功返回' if d.get('ok') is False else None))
        elif t == T_AUDIT:
            r = _num(d.get('ratio'))
            emit(i, 'audit', u'事实核查', 'audit', detail=u'未核验率 %s · 样本 %d 条 · 未核验 %d 条' % (
                (u'%.0f%%' % (r * 100)) if r is not None else u'—',
                _int(d.get('total')), _int(d.get('unverified'))),
                meta={'ratio': r, 'total': _int(d.get('total')),
                      'unverified': _int(d.get('unverified'))})
        elif t == T_END:
            summ = d.get('summary') or {}
            emit(i, 'end', u'结果交付', 'end',
                 state=('done' if d.get('ok') else 'failed'),
                 ms=None,
                 detail=u'%s · 总耗时 %s · %d tokens · %d 轮%s' % (
                     _clip(d.get('outcome') or 'ok', 18),
                     _fmt_sec((_num(summ.get('elapsed_ms')) or 0) / 1000.0),
                     _int(summ.get('total_tokens')), _int(summ.get('iterations')),
                     u' · 工具 %d 次' % _int(summ.get('tool_calls'))),
                 meta={'total_ms': _int(summ.get('elapsed_ms')),
                       'tokens': _int(summ.get('total_tokens')),
                       'errors': _int(summ.get('errors')),
                       'iterations': _int(summ.get('iterations'))},
                 error=(_clip(d.get('outcome'), 60) if d.get('ok') is False else None))
    if not idx['done'] and steps:
        steps[-1]['state'] = 'running'
    return steps


def build_issues(idx):
    """把事件流里的异常汇总成「问题报错」清单（只报事实，不猜）。"""
    out = []

    def add(level, kind, title, detail=u'', hint=u'', at=u''):
        out.append({'level': level, 'kind': kind, 'title': _clip(title, 90),
                    'detail': _clip(detail, 400), 'hint': _clip(hint, 160), 'at': _clip(at, 40)})

    # 1) 流水线阶段失败
    for rec in idx['stages']:
        if rec['state'] == 'failed':
            add('error', u'阶段失败', u'阶段 %d · %s' % (rec['no'], rec['name']),
                u'%s%s' % (_clip(rec['error'], 200) or u'阶段返回 ok=false',
                           (u' · 用时 %s' % _fmt_sec(rec['sec'])) if rec['sec'] is not None else ''),
                u'检查该阶段的模型可用性与输入长度；上游会把失败阶段放进 summary.failed_stages',
                u'阶段 %d' % rec['no'])

    # 2) run_end 汇总里报告的失败阶段 / 错误数
    summ = (idx.get('end_data') or {}).get('summary') or {}
    failed = summ.get('failed_stages') or []
    have_stage_issue = any(i['kind'] == u'阶段失败' for i in out)
    if failed and not have_stage_issue:
        add('error', u'阶段失败', u'运行汇总报告 %d 个失败阶段' % len(failed),
            u'failed_stages = %s' % _clip(u'、'.join([str(x) for x in failed]), 200),
            u'看 data.tool 后台日志里对应阶段的报错', u'summary')
    nerr = _int(summ.get('errors'))
    if nerr > 0 and not have_stage_issue:
        add('error', u'运行错误', u'运行期间记录到 %d 个错误' % nerr,
            u'run_end.summary.errors = %d' % nerr, u'点开本次运行的原始事件排查', u'summary')

    # 3) 整体失败
    if idx['done'] and idx['ok'] is False:
        add('error', u'运行失败', u'本次运行未成功结束',
            u'outcome = %s' % _clip((idx.get('end_data') or {}).get('outcome'), 120),
            u'先看工具调用是否失败 / 阶段是否报错', u'run_end')

    # 4) 工具调用失败
    for _, d in idx['tool_pairs']:
        if _norm_status(d.get('status')) == 'failed':
            add('error', u'工具调用失败',
                u'第 %d 轮 · %s' % (_int(d.get('iteration'), 1) or 1, _clip(d.get('tool') or u'工具', 40)),
                u'status=%s · %sms · %s' % (_clip(d.get('status'), 20), _int(d.get('duration_ms')),
                                            _clip(d.get('preview'), 160) or u'无预览'),
                u'确认该工具/数据源可用；必要时在助手侧加重试',
                u'第 %d 轮' % (_int(d.get('iteration'), 1) or 1))

    # 5) 降级回退
    for _, d in idx['fallback_pairs']:
        add('warn', u'降级重试', u'触发兜底重试', u'mode = %s' % (_clip(d.get('mode'), 60) or u'—'),
            u'主路径失败后走了兜底，结果可用但链路非最优', u'fallback')
    for _, d in idx['degraded_pairs']:
        add('warn', u'工具协议降级', u'原生工具调用不可用，未识别 %d 次' % _int(d.get('misses')),
            u'model = %s' % (_clip(d.get('model'), 60) or u'—'),
            u'该模型不支持原生 tool_calls，改走文本协议解析', u'degraded')

    # 6) 终稿收尾（2026-09-18 拆分：真截断 vs 达轮数上限收尾）
    for _, d in idx['fin_done_pairs']:
        _trunc, _forced = _final_flags(d)
        if _trunc:
            add('warn', u'输出截断', u'终稿被截断（truncated=true）',
                u'content_len = %d' % _int(d.get('content_len')),
                u'上下文或 max_tokens 触顶，考虑缩短输入', u'finalize')
        elif _forced:
            add('info', u'达轮数上限收尾', u'用满迭代预算后强制收尾（正常流程）',
                u'content_len = %d' % _int(d.get('content_len')),
                u'不是截断：终稿是完整收束的，只是没再继续迭代', u'finalize')
        if d.get('ok') is False:
            add('error', u'终稿失败', u'finalize.done 报告 ok=false',
                u'content_len = %d' % _int(d.get('content_len')),
                u'看后台日志里 finalize 阶段的异常', u'finalize')

    # 7) 事实溯源（ratio = 未核验率，越高越差；2026-09-18 修正方向）
    for _, d in idx['audit_pairs']:
        ratio = _num(d.get('ratio'))
        unv = _int(d.get('unverified'))
        total = _int(d.get('total'))
        if ratio is not None and ratio > FACT_UNVERIFIED_WARN:
            add('warn', u'数字缺少可核查来源', u'未核验率 %.0f%%（阈值 %.0f%%）' % (
                    ratio * 100, FACT_UNVERIFIED_WARN * 100),
                u'样本 %d 条 · 未核验 %d 条' % (total, unv),
                u'答复中的关键数字缺引用，存在编造风险；需补证据链', u'fact_audit')
        elif unv > 0:
            add('info', u'存在未核验条目', u'未核验 %d / %d 条' % (unv, total),
                u'上游 ratio = %s · 样本 %d 条' % (
                    (u'%.0f%%' % (ratio * 100)) if ratio is not None else u'—', total),
                u'未核验不等同于错误，但引用链不完整', u'fact_audit')

    order = {'error': 0, 'warn': 1, 'info': 2}
    out.sort(key=lambda x: order.get(x['level'], 9))
    return out


# ---------------------------------------------------------------- 拓扑（Spec）
def _topology_signature(nodes, edges):
    raw = '|'.join([n['id'] for n in nodes]) + '#' + \
          '|'.join(['%s>%s' % (e['from'], e['to']) for e in edges])
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]


_STAGE_LABEL = {
    'data': '入口', 'coordinator': '调度', 'worker': '推理循环',
    'tool': '工具', 'reviewer': '核查', 'output': '交付',
}


def build_spec(trace):
    """由一次运行的事件流生成可视化器的 Spec。"""
    idx = index_events(trace)
    nodes = []
    edges = []
    stages = {}

    def add(nid, name, role, stage, **kw):
        it = {'id': nid, 'name': name, 'role': role, 'stage': stage}
        it.update(kw)
        nodes.append(it)
        stages.setdefault(stage, _STAGE_LABEL.get(role, '阶段'))
        return it

    def link(a, b, **kw):
        edges.append(dict({'id': 'e_%s_%s' % (a, b), 'from': a, 'to': b}, **kw))

    s = 0
    msg = _clip(idx['user_message'], 220)
    add('start', '用户提问', 'data', s,
        desc=msg or '（未记录提问内容）',
        artifact='user_message',
        outputs=['提问文本'],
        tip=msg or '用户提问')
    prev = 'start'

    s += 1
    model = idx['model'] or '未知模型'
    add('agent', 'AI 助手', 'coordinator', s,
        desc='%s · %s' % (idx['provider'] or 'provider?', model),
        en=(model or '').upper(),
        artifact='agent_config',
        outputs=['决策', '工具调用'],
        tip='data.tool AI 助手 · %s' % model)
    link(prev, 'agent')
    prev = 'agent'
    fan_in_all = [prev]          # 下一列的扇入来源（并行组时为多个节点）

    if idx['mode'] == 'pipeline' and idx['stages']:
        # 形态 B：多阶段流水线。连续的并行工作组（市场分析师 / 量化建模师 /
        # 风控合规官）合并到同一列：上一列扇出 → 并行 → 下一列扇入。
        fan_in = [prev]
        for block in _stage_blocks(idx['stages']):
            s += 1
            ids = []
            for rec in block['members']:
                nid = 'stage%d' % rec['no']
                nm = rec['name']
                role = 'reviewer' if REVIEW_HINT.search(nm) else (
                    'output' if OUTPUT_HINT.search(nm) else 'worker')
                add(nid, nm, role, s,
                    desc='第 %d 阶段 · %s / %s%s' % (rec['no'], rec['provider'] or '-',
                                                    rec['model'] or '-',
                                                    ' · 预算 %d 轮' % rec['iters'] if rec['iters'] else ''),
                    en=(rec['model'] or '').upper()[:26],
                    artifact='pipeline.stage',
                    outputs=['阶段产出'],
                    tip='%s · %s' % (nm, rec['model'] or '未知模型'))
                ids.append(nid)
            if block['merged']:
                stages[s] = u'并行工作组'
            for a in fan_in:
                for b in ids:
                    link(a, b)
            fan_in = ids
        prev = fan_in[-1]
        fan_in_all = fan_in
    else:
        if idx['fallbacks'] or idx['degraded']:
            s += 1
            miss = 0
            for _, d in idx['degraded_pairs']:
                miss = max(miss, _int(d.get('misses')))
            add('fallback', '降级回退', 'reviewer', s,
                desc='原生工具协议不可用时的兜底重试',
                artifact='fallback path',
                metrics=None,
                tip='降级回退（misses=%d）' % miss if miss else '降级回退')
            link(prev, 'fallback')
            prev = 'fallback'

        iters = idx['iters'] or ([1] if idx['llm_pairs'] else [])
        for i in iters:
            s += 1
            iid = 'iter%d' % i
            add(iid, '推理 #%d' % i, 'worker', s,
                desc='第 %d 轮模型推理' % i,
                artifact='llm_call',
                outputs=['回复', '工具请求'],
                tip='第 %d 轮推理' % i)
            link(prev, iid)
            prev = iid

            # 工具调用是本轮推理的附属动作：与之同列，不额外占用阶段（图更紧凑）
            for k, d in enumerate(idx['tools_by_iter'].get(i, []), 1):
                tid = 'tool%d_%d' % (i, k)
                tname = _clip(d.get('tool') or '工具', 24)
                add(tid, tname, 'tool', s,
                    desc='第 %d 轮的工具调用' % i,
                    artifact=tname,
                    tip=tname)
                link(iid, tid)

        # 终稿合成：收束推理、产出最终答复（自带 duration_ms）
        if idx['fin_dones']:
            s += 1
            ms = _int(idx['fin_done_pairs'][-1][1].get('duration_ms'))
            add('finalize', '终稿合成', 'worker', s,
                desc='收束推理，产出最终答复',
                artifact='finalize.done',
                outputs=['最终答复'],
                tip='终稿合成 · %sms' % ms)
            link(prev, 'finalize')
            prev = 'finalize'
        fan_in_all = [prev]

    if idx['audits']:
        s += 1
        add('audit', '事实核查', 'reviewer', s,
            desc='对产出做事实一致性核查',
            artifact='fact_audit',
            tip='事实核查')
        for a in fan_in_all:
            link(a, 'audit')
        prev = 'audit'
        fan_in_all = [prev]

    s += 1
    add('end', '结果交付', 'output', s,
        desc='汇总并返回最终答复',
        artifact='run_end',
        inputs=['推理结果'],
        tip='结果交付')
    for a in fan_in_all:
        link(a, 'end')

    stage_names = [stages.get(k, '阶段 %d' % (k + 1)) for k in range(s + 1)]
    sec = idx['elapsed']
    run_block = {}
    if sec is not None:
        run_block['seconds'] = round(sec, 1)
    if idx['tokens']:
        run_block['tokens'] = idx['tokens']

    if idx['mode'] == 'pipeline':
        title = 'Agent 流水线 · %d 阶段' % len(idx['stages'])
        if idx['model']:
            title += ' · %s' % idx['model']
    else:
        title = 'AI 助手运行轨迹'
        if idx['model']:
            title = 'AI 助手运行轨迹 · %s' % idx['model']
    spec = {
        'version': 1,
        'title': title,
        'subtitle': 'DATA.TOOL ASSISTANT TRACE',
        'stages': [{'name': nm} for nm in stage_names],
        'nodes': nodes,
        'edges': edges,
        'run': run_block,
        'source': 'datatool-trace',
    }
    spec['rev'] = _topology_signature(nodes, edges)
    spec['run_id'] = idx['run_id']
    spec['mode'] = idx['mode']
    return spec


# ---------------------------------------------------------------- 状态快照
def _state_of_end(idx):
    if not idx['done']:
        return 'running', '执行中…'
    if idx['ok']:
        return 'done', '运行结束 · 正常返回'
    out = (idx['end_data'] or {}).get('outcome') or '异常'
    return 'failed', '运行结束 · %s' % out


def build_state(trace, base=DEFAULT_BASE, cid='', keepalive=True, run_meta=None):
    """由事件流生成「当前状态快照」，字段名照顾可视化器的自动识别。"""
    idx = index_events(trace)
    agents = []
    links = []
    logs = []
    summary = {}

    def A(aid, name, status, detail=''):
        it = {'id': aid, 'name': name, 'status': status}
        if detail:
            it['detail'] = detail
        agents.append(it)

    def L(a, b, state):
        links.append({'from': a, 'to': b, 'state': state})

    all_iters = set(idx['iters'])
    if idx['llm_pairs']:
        all_iters.add(1)
    max_iter = max(all_iters) if all_iters else 0
    done = idx['done']

    # ---- 用户提问 ----
    if idx['starts']:
        A('start', '用户提问', 'done', _clip(idx['user_message'], 60) or '已收到')
    else:
        A('start', '用户提问', 'pending', '等待提问')

    # ---- AI 助手（调度层） ----
    n_tool = len(idx['tool_pairs'])
    if idx['llm_pairs'] or idx['stages'] or done:
        bits = []
        if idx['skill']:
            bits.append('skill=%s' % _clip(idx['skill'], 18))
        if idx['mode'] == 'pipeline':
            bits.append('%d 个阶段' % len(idx['stages']))
        else:
            if idx['max_iterations']:
                bits.append('预算 %d 轮' % idx['max_iterations'])
            bits.append('已推理 %d 轮' % len(all_iters))
        if n_tool:
            bits.append('工具 %d 次' % n_tool)
        A('agent', 'AI 助手', 'done' if done else 'running', ' · '.join(bits))
        L('start', 'agent', 'done')
    else:
        A('agent', 'AI 助手', 'running', '解析运行配置…')
        L('start', 'agent', 'live')

    # ---- 降级回退 ----
    if idx['fallbacks'] or idx['degraded']:
        modes = []
        for _, d in idx['fallback_pairs']:
            if d.get('mode'):
                modes.append(_clip(d['mode'], 20))
        for _, d in idx['degraded_pairs']:
            modes.append('原生工具降级·misses=%d' % _int(d.get('misses')))
        A('fallback', '降级回退', 'done', ' · '.join(modes) or '已触发')
        L('agent', 'fallback', 'done')

    # ---- 推理轮次 / 流水线阶段 ----
    prev = 'fallback' if ('fallback' in [a['id'] for a in agents]) else 'agent'
    fan_in_all = [prev]          # 下一列的扇入来源（并行组时为多个节点）
    if idx['mode'] == 'pipeline' and idx['stages']:
        fan_in = [prev]
        for block in _stage_blocks(idx['stages']):
            ids = []
            merged = block['merged']
            # 并行组呈现：组内任一成员在跑 → 其余未完成的成员同时显示「进行中」
            group_running = merged and any(r['state'] == 'running' for r in block['members'])
            for rec in block['members']:
                nid = 'stage%d' % rec['no']
                stt = rec['state']
                if merged and stt not in ('done', 'failed'):
                    stt = 'running' if group_running else 'pending'
                bits = []
                if rec['sec'] is not None:
                    bits.append(_fmt_sec(rec['sec']))
                elif stt == 'running':
                    bits.append('并行执行中…')
                if rec['model']:
                    bits.append(_clip(rec['model'], 18))
                if rec['iters']:
                    bits.append('%d 轮' % rec['iters'])
                if rec['chars']:
                    bits.append('%d 字符' % rec['chars'])
                dt = ' · '.join(bits)
                if rec['state'] == 'failed':
                    dt = _clip(rec['error'], 48) or '阶段失败'
                A(nid, rec['name'], stt, dt)
                ids.append(nid)
            srcs = fan_in
            any_run = any(True for r in block['members'] if r['state'] == 'running') or group_running
            all_done = all(r['state'] == 'done' for r in block['members'])
            lstate = 'done' if all_done else ('live' if any_run else 'done')
            for a in srcs:
                for b in ids:
                    L(a, b, lstate)
            fan_in = ids
        prev = fan_in[-1]
        fan_in_all = fan_in
    else:
        for i in sorted(all_iters):
            iid = 'iter%d' % i
            llms = [d for _, d in idx['llm_pairs'] if (_int(d.get('iteration'), 1) or 1) == i]
            is_last = (i == max_iter)
            if done or not is_last:
                st, dt = 'done', ''
            else:
                st, dt = 'running', '模型推理中…'
            if llms:
                last = llms[-1]
                u = last.get('usage') or {}
                parts = ['返回 %s 字符' % _int(last.get('content_len'))]
                if u.get('total_tokens'):
                    parts.append('%d tokens' % _int(u.get('total_tokens')))
                if last.get('has_tool_calls'):
                    parts.append('请求工具')
                if last.get('native') is False:
                    parts.append('文本协议')
                ms = _num(last.get('duration_ms'))
                if ms:
                    parts.append(_fmt_sec(ms / 1000.0))
                dt = ' · '.join(parts)
            A(iid, '推理 #%d' % i, st, dt)
            L(prev, iid, 'done' if (done or not is_last) else 'live')
            prev = iid
            for k, d in enumerate(idx['tools_by_iter'].get(i, []), 1):
                tid = 'tool%d_%d' % (i, k)
                tname = _clip(d.get('tool') or '工具', 24)
                # 画布上的节点说明要短：完整 preview 走日志，避免相邻节点外框重叠
                parts = []
                if d.get('duration_ms') is not None:
                    parts.append(_fmt_sec(_int(d.get('duration_ms')) / 1000.0))
                if d.get('cached'):
                    parts.append('命中缓存')
                pv = _clip(d.get('preview'), 16)
                if pv:
                    parts.append(pv)
                st = _norm_status(d.get('status'))
                A(tid, tname, st, ' · '.join(parts))
                L(iid, tid, 'done' if st == 'done' else 'failed')

        # ---- 终稿合成 ----
        if idx['fin_dones']:
            fd = idx['fin_done_pairs'][-1][1]
            ms = _int(fd.get('duration_ms'))
            okf = fd.get('ok')
            stt = 'done' if okf in (None, True) else 'failed'
            bits = []
            if ms:
                bits.append(_fmt_sec(ms / 1000.0))
            bits.append('%d 字符' % _int(fd.get('content_len')))
            _trunc, _forced = _final_flags(fd)
            if _trunc:
                bits.append('输出截断')
            elif _forced:
                bits.append('达轮数上限收尾')
            A('finalize', '终稿合成', stt, ' · '.join(bits))
            L(prev, 'finalize', 'done' if stt == 'done' else 'failed')
            prev = 'finalize'
        fan_in_all = [prev]

    # ---- 事实核查 ----
    if idx['audits']:
        d = idx['audit_pairs'][-1][1]
        ratio = _num(d.get('ratio'))
        total = _int(d.get('total'))
        unv = _int(d.get('unverified'))
        dt = '样本 %d 条 · 未核验 %d 条' % (total, unv)
        st = 'done'
        if ratio is not None:
            # ratio 是未核验率：高于阈值才降级（旧实现按"通过率"判方向，反了）
            dt = '未核验率 %.0f%% · 样本 %d 条 · 未核验 %d 条' % (ratio * 100, total, unv)
            st = 'failed' if ratio > FACT_UNVERIFIED_WARN else 'done'
        A('audit', '事实核查', st, dt)
        for a in fan_in_all:
            L(a, 'audit', 'done' if st == 'done' else 'failed')
        prev = 'audit'
        fan_in_all = [prev]

    # ---- 结果交付 ----
    st, dt = _state_of_end(idx)
    if done:
        if idx['elapsed'] is not None:
            dt = '总耗时 %s' % _fmt_sec(idx['elapsed'])
            dt += ' · %d 个阶段' % len(idx['stages']) if idx['mode'] == 'pipeline' \
                else ' · %d 轮推理' % len(all_iters)
        if idx['tokens']:
            dt += ' · %d tokens' % idx['tokens']
    A('end', '结果交付', st, dt)
    for a in fan_in_all:
        L(a, 'end', 'done' if done else 'live')

    # ---- 汇总指标 ----
    if idx['elapsed'] is not None:
        summary['seconds'] = round(idx['elapsed'], 1)
    if idx['tokens']:
        summary['tokens'] = idx['tokens']
    summary['note'] = 'DATA.TOOL · %s · %s' % (
        base.replace('http://', ''),
        'PIPELINE %d 阶段' % len(idx['stages']) if idx['mode'] == 'pipeline' else '无需流水线')
    if idx['model']:
        summary['chip'] = '%s / %s' % (idx['provider'] or '-', idx['model'])

    # ---- 日志（按 seq 顺序、前缀稳定，方便前端增量去重） ----
    def mk(actor, text):
        logs.append({'actor': actor, 'message': text})

    for e in idx['events']:
        t = e.get('type')
        d = e.get('data') or {}
        if t == T_START:
            mk('用户提问', '收到提问：%s' % (_clip(d.get('user_message'), 90) or '—'))
            mk('AI 助手', '模型 %s（%s）' % (d.get('model') or '?', d.get('provider') or '?'))
        elif t == T_CONFIG:
            mk('AI 助手', '运行配置 · 最多 %d 轮 · plan=%s' %
               (_int(d.get('max_iterations')), 'on' if d.get('plan') else 'off'))
        elif t == T_LLM:
            u = d.get('usage') or {}
            mk('推理 #%d' % (_int(d.get('iteration'), 1) or 1),
               '第 %d 轮返回 %d 字符 · %d tokens%s' %
               (_int(d.get('iteration'), 1) or 1, _int(d.get('content_len')),
                _int(u.get('total_tokens')),
                ' · 请求工具' if d.get('has_tool_calls') else ''))
        elif t == T_TOOL:
            mk(_clip(d.get('tool') or '工具', 20),
               '%s · %sms · %s' % (_clip(d.get('status') or 'done', 12),
                                   _int(d.get('duration_ms')),
                                   _clip(d.get('preview'), 70) or '无预览'))
        elif t == T_FALLBACK:
            mk('降级回退', '触发兜底重试 · mode=%s' % (_clip(d.get('mode'), 24) or '—'))
        elif t == T_DEGRADED:
            mk('降级回退', '原生工具调用降级 · 未识别 %d 次' % _int(d.get('misses')))
        elif t == T_AUDIT:
            r = _num(d.get('ratio'))
            mk('事实核查', '未核验率 %s · 样本 %d 条 · 未核验 %d 条' %
               (('%.0f%%' % (r * 100)) if r is not None else '—',
                _int(d.get('total')), _int(d.get('unverified'))))
        elif t == T_END:
            mk('结果交付', '运行结束 · %s · 耗时 %.1fs · %d tokens' %
               (_clip(d.get('outcome') or 'ok', 20),
                idx['elapsed'] if idx['elapsed'] is not None else 0.0,
                idx['tokens']))

    # ---- 拓扑：首次（或拓扑变化）时把 spec 一并下发 ----
    spec = build_spec(trace)
    rev = spec['rev']
    include_spec = True
    if cid:
        with _SERVED_LOCK:
            include_spec = _SERVED.get(cid) != rev
            if include_spec:
                _SERVED[cid] = rev
                if len(_SERVED) > 200:
                    for k in list(_SERVED.keys())[:40]:
                        _SERVED.pop(k, None)

    timeline = build_timeline(idx)
    issues = build_issues(idx)
    sum_dur = 0.0
    n_dur = 0
    for x in timeline:
        if x['dur'] is not None:
            sum_dur += x['dur']
            n_dur += 1
    payload = {
        'run': idx['run_id'],
        'rev': rev,
        't': round(idx['elapsed'], 3) if idx['elapsed'] is not None else 0,
        'phase': 'done' if done else 'running',
        'done': (done and not keepalive),
        'agents': agents,
        'links': links,
        'summary': summary,
        'logs': logs,
        'mode': idx['mode'],
        'timeline': timeline,
        'issues': issues,
        'issues_summary': {
            'error': len([x for x in issues if x['level'] == 'error']),
            'warn': len([x for x in issues if x['level'] == 'warn']),
        },
        'totals': {
            'steps': len(timeline),
            'timed': n_dur,
            'sum_s': round(sum_dur, 3),
            'elapsed_s': round(idx['elapsed'], 3) if idx['elapsed'] is not None else None,
            'tokens': idx['tokens'],
            'errors': _int((idx.get('end_data') or {}).get('summary', {}).get('errors')),
        },
    }
    if include_spec:
        payload['spec'] = spec
    return payload


def build_state_auto(base=DEFAULT_BASE, run=None, cid='', keepalive=True):
    """一步到位：拉取 + 快照。run 为空时自动跟随最新一次运行。"""
    trace = fetch_trace(base, run)
    return build_state(trace, base=base, cid=cid, keepalive=keepalive)


# ---------------------------------------------------------------- CLI
def main(argv=None):
    import argparse
    import sys as _sys

    ap = argparse.ArgumentParser(description='data.tool AI 助手运行轨迹适配器（本地直读）')
    ap.add_argument('--base', default='', help='已废弃：本地模式忽略')
    ap.add_argument('--run', default='')
    ap.add_argument('--list', action='store_true', help='列出最近运行')
    ap.add_argument('--spec', action='store_true', help='只输出 Spec')
    ap.add_argument('--state', action='store_true', help='只输出状态快照')
    ap.add_argument('--json', default='', help='把结果写入文件')
    args = ap.parse_args(argv)

    try:
        if args.list:
            runs = list_runs(args.base, 20)
            print('共 %d 次运行：' % len(runs))
            for r in runs:
                print('  %-24s %-18s ok=%-5s %s' %
                      (r['run_id'], r['model'] or '-', r['ok'], _clip(r['user_message'], 40)))
            return 0

        trace = fetch_trace(args.base, args.run or None)
        idx = index_events(trace)
        print('run_id     : %s' % idx['run_id'])
        print('模型        : %s / %s' % (idx['provider'], idx['model']))
        print('提问        : %s' % _clip(idx['user_message'], 60))
        print('事件数      : %d' % len(idx['events']))
        print('推理轮次    : %s' % (idx['iters'] or '-'))
        print('工具调用    : %d 次' % len(idx['tool_pairs']))
        print('降级/核查   : %d / %d' % (len(idx['fallbacks']) + len(idx['degraded']), len(idx['audits'])))
        print('耗时/tokens : %s s / %s' % (idx['elapsed'], idx['tokens']))
        print('结束        : %s' % ('未结束' if not idx['done'] else ('ok' if idx['ok'] else 'failed')))
        print('形态        : %s%s' % (idx['mode'],
              ('（%d 阶段）' % len(idx['stages'])) if idx['stages'] else ''))
        tl = build_timeline(idx)
        print('逐步计时    : 共 %d 步' % len(tl))
        for n, x in enumerate(tl, 1):
            print('   %2d %-14s %-8s %-13s %-9s %s' % (
                n, _clip(x['name'], 12), x['state'],
                _fmt_sec(x['dur']), {'exact': '精确', 'gap': '推算', 'none': '—'}[x['src']],
                't0=%s' % (_fmt_sec(x['t0']) if x['t0'] is not None else '—')))
        iss = build_issues(idx)
        print('问题报错    : %d 项（error %d / warn %d）' % (
            len(iss), len([x for x in iss if x['level'] == 'error']),
            len([x for x in iss if x['level'] == 'warn'])))
        for x in iss:
            print('   [%-5s] %s -- %s' % (x['level'], x['title'], x['detail'][:70]))

        if args.spec:
            payload = build_spec(trace)
        elif args.state:
            payload = build_state(trace, base=args.base)
        else:
            payload = {'spec': build_spec(trace),
                       'state': dict(build_state(trace, base=args.base), spec=None)}
            payload['state'].pop('spec', None)
        txt = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.json:
            with open(args.json, 'w', encoding='utf-8') as fh:
                fh.write(txt)
            print('已写入 %s' % args.json)
        else:
            print('-' * 66)
            print(txt[:6000])
    except Exception as exc:                                   # noqa: BLE001
        print('失败：%s' % exc, file=_sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    import sys as _sys
    _sys.exit(main())
