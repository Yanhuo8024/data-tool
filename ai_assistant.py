#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_assistant.py - AI Conversation Assistant Engine

Features:
1. Natural language understanding (intent recognition + parameter extraction)
2. Data context awareness (dynamic injection of current data overview)
3. Structured report generation (Markdown format)
4. Multi-turn conversation memory (context inheritance + coreference resolution)
5. Autonomous analysis (read data -> statistics -> LLM deep thinking)
6. DID regression analysis (Difference-in-Differences for panel data)
"""

import re
import os
import json
import time
import logging
import threading
from typing import Dict, Any, List, Optional, Iterator
from dataclasses import dataclass, field
from datetime import datetime, date

logger = logging.getLogger(__name__)

# 对话历史持久化文件（进程重启不丢失）
HISTORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'chat_history.json')
# 本文件是"运行时缓存"：只保留最近 100 条、会被截断。完整的本地留档在
# chat_archive 维护的归档目录（默认 data/chat_history/YYYY-MM-DD/<会话>.json|.md），
# 由 _save_history() 顺带合并写入，不受条数上限影响。
_HISTORY_FIELDS = ('role', 'content', 'timestamp', 'metadata', 'conversation_id', 'ts')

# ==================== P0-1：思维链标签统一剥离 ====================
# 实现已拆到 text_clean.py（纯文本工具，且本文件受 R1 行数门禁约束）；
# 此处原样再导出，保持 `from ai_assistant import strip_thinking` 的既有调用点不变。
from text_clean import strip_thinking  # noqa: E402,F401
# 业务能力门面（券商能力对齐）：知识库引用增强 / 智能运营分流（详见 business_agent）
from business_agent import kb_answer, try_ops_reply  # noqa: E402,F401
from kb_cite import cite_id  # noqa: E402,F401


def _vision_egress_blocked(message: str) -> Optional[Dict[str, Any]]:
    """识图出境闸门（转发）：返回 None 放行 / dict 为"图片不外发"的最终答复。

    实现已迁至 egress_policy.vision_blocked_for()（本文件受 R1 行数棘轮约束），
    与 ToolRegistry 出境闸门同源；策略不可读时按默认放行并留痕，不误伤正常识图。
    """
    from egress_policy import vision_blocked_for
    return vision_blocked_for(message)


# ==================== P0-3：提示词复述（echo）护栏 ====================
# 实测（2026-09-19）：模型偶尔不把"思考"写进 <thinking>，而是把系统提示词的**元结构**
# 当正文输出，形如：
#     1. **分析请求：**
#        * **用户问题：** "你好，简单介绍一下你能做什么"
#        * **角色设定：** 靠谱的数据分析与金融顾问，讲人话…
# strip_thinking 只能剥标签块，拦不住这种纯文本复述。更糟的是它会进会话历史，
# 下一轮作为"助手历史"喂回模型 → 模型看到自己这么写，继续复述（自我强化）。
# 这里提供统一判定：只扫开头（避免误伤正文里正常出现的相关词），命中 ≥2 个元标签才算。
_ECHO_MARKERS = ('分析请求', '角色设定', '用户问题：', '任务要求', '输出要求',
                 '回答要求', '思考过程', '思维链', '系统提示', '提示词', '角色定位')


def looks_like_prompt_echo(text: str) -> bool:
    """回答是否为"提示词复述"（元结构泄漏）。空文本返回 False。"""
    if not text:
        return False
    head = str(text)[:500]
    return sum(1 for m in _ECHO_MARKERS if m in head) >= 2


def _clean_result(payload: Dict[str, Any]) -> Dict[str, Any]:
    """对响应体做统一后处理（当前仅剥离 thinking 标签），保持结构不变。"""
    if isinstance(payload, dict) and isinstance(payload.get('reply'), str):
        payload = dict(payload)
        payload['reply'] = strip_thinking(payload['reply'])
    return payload


# ==================== P1：联网必要性闸门 ====================
# 仅当问题"需要最新/外部信息"时才联网搜索；闲聊、算术、功能指引等直接由 LLM 或本地回答，
# 避免"今天天气/请继续/1+1"这类问题无谓触发搜索（省时省额度、降低思考过度感）。
_WEB_REQUIRED_KW = (
    '最新', '现在', '目前', '当前', '今天', '今年', '最近', '实时', '今日', '截至',
    '股价', '估值', '市盈率', '市净率', '行情', '走势', '点位', '利率', '汇率', '房价',
    '政策', '新闻', '动态', '发布', '公告', '财报', '预期', '预测', '展望', '价格',
    '排名', '对比', '数据', '统计', '市场规模', '增长率', '2024', '2025', '2026', '2027',
)
# 明确不需要联网的意图（本地即可回答）
_NO_WEB_INTENTS = ('operation', 'troubleshoot', 'greeting', 'help')


def _needs_web_search(message: str, router: dict = None) -> bool:
    """判断该问题是否值得联网搜索（P1 闸门）。

    - 功能指引/排障类意图 → 不联网（本地帮助）
    - 命中时效性/外部信息关键词 → 联网
    - 其余（闲聊、算术、概念直觉题）→ 不联网，交给 LLM 直接答
    """
    if not message:
        return False
    intent = (router or {}).get('intent')
    if intent in _NO_WEB_INTENTS:
        return False
    # nlq（自然语言查数）：查的是本地数据，不该联网。此前 nlq 不在白名单，而
    # _WEB_REQUIRED_KW 含「统计/数据/对比/排名」等宽泛词 → "统计缺失值的个数"这类
    # 本地查询被误判为需联网（实测走了 web_search 并返回"未找到相关网页"）。
    # 仅当该句带金融属性（行情/估值等实时数据）时才保留联网判定。
    if intent == 'nlq' and not (router or {}).get('finance', {}).get('is_finance'):
        return False
    # 纯算术/计算类：本地计算，不联网
    if detect_special_intent_safe(message) == 'computation':
        return False
    # 本地可计算（描述统计 / 相关性 / 基金指标 / 数值运算）→ 直接真实计算，不联网。
    # 否则「帮我做个描述统计」会因 _WEB_REQUIRED_KW 里的宽泛词「统计」被误判为需联网，
    # 而 real_compute 明明可以本地算出真值（实测该句走了 web_search 并联网失败）。
    try:
        from real_compute import infer_compute_intent
        if infer_compute_intent(message):
            return False
    except Exception:
        pass
    # 明确的时效/外部信息需求 → 联网
    if any(w in message for w in _WEB_REQUIRED_KW):
        return True
    return False


# ==================== P0-3：高价值深度报告触发词 ====================
# 这些说法隐含"要有条理的完整分析"，无文件时也应放行到联网/Agent 深度链路，
# 而不是落入"请上传文件"死胡同。
_DEEP_REPORT_KW = (
    '研究报告', '完整分析', '完整报告', '全面分析', '深度研究', '深度分析', '尽调',
    '调研报告', '研究一下', '全面评估', '系统分析', '整体分析', '写一份', '做一份', '做一份报告',
)


def _wants_deep_report(message: str) -> bool:
    """是否显式要求"完整/有条理的深度报告"（P0-3）。"""
    return bool(message) and any(k in message for k in _DEEP_REPORT_KW)


# ==================== 方法论/通用问法识别 ====================
# "如何做好数据分析""怎么做数据清洗"这类是方法论问题，不是"对某份数据做分析"，
# 正则识别器会因命中"分析/清洗"误判为 analyze_data，进而在无文件时要求上传数据。
_METHOD_MARKERS = ('如何做', '怎么做', '怎么做好', '如何做好', '如何开展', '如何入门',
                   '怎么学', '如何学习', '入门', '方法论', '流程', '步骤', '最佳实践',
                   '有哪些方法', '有什么方法', '是怎么做', '如何提升', '怎么提升')


def _is_methodology_question(message: str) -> bool:
    """是否属于"方法论/通用怎么做"类问题（不针对具体数据文件）。"""
    if not message:
        return False
    if any(m in message for m in ('什么是', '是什么', '什么意思', '含义', '定义')):
        return True
    return any(m in message for m in _METHOD_MARKERS) and any(
        d in message for d in ('数据', '分析', '清洗', '建模', '统计', '可视化', '报告'))


# 概念型问法标记：命中则说明用户要的是概念解释，知识库可以正常应答
_CONCEPT_MARKERS = ('什么是', '是什么', '什么意思', '含义', '定义', '概念', '指的是', '怎么算', '如何计算')


def _kb_should_yield(message: str, intent_router: dict, current_file: str = None) -> bool:
    """知识库是否应让路（不让路 = 允许知识条目抢答）。同步/流式共用，消除双路径漂移。

    让路条件：
      ① 路由判为功能指引/排障（怎么用/怎么导出/报错）→ 交给 help 分支本地清单；
      ② 路由**明确判为「分析」**（置信度 ≥ 0.8）且非概念型问法 → 交给分析/金融/Agent 链路。
         （实测"为什么最近大盘跌了"被判 analysis，却被知识条目截胡成一段概念解释。）
      ③ **挂了数据文件**且消息含真实分析信号 → 让路。实测"这个数据文件里有哪些字段？
         先做一次描述统计"路由只给到 analysis/0.625，够不到 ② 的 0.8 门槛，结果被知识库
         截胡，答成"什么是数据分析"的概念解释 + 一句"我还没看到你上传的数据文件"——
         用户带着文件提问却看不到自己的数据。挂了文件就该走文件链路。
    """
    if not intent_router:
        return False
    intent = intent_router.get('intent')
    if intent in ('operation', 'troubleshoot'):
        return True
    if intent == 'analysis':
        _concept = any(m in (message or '') for m in _CONCEPT_MARKERS)
        if float(intent_router.get('confidence') or 0) >= 0.8:
            return not _concept
        if current_file and _has_analysis_signal(message) and not _concept:
            return True
    return False


# ==================== P0-2：派发以 router 判定为准 ====================
# 旧正则 IntentRecognizer 已降级为兜底；正常路由以 intent_router 的 action 为准，
# 消除"两套意图系统判定漂移"（router 判 operation，实际却按 chat 联网等）。
_KNOWN_ACTIONS = ('analyze_data', 'create_chart', 'nlq', 'knowledge', 'help',
                  'chat', 'greeting', 'correlation', 'clean_data', 'train_model')

# 真实"分析信号"词：出现这些才认为用户确实要做（数据）分析。
# 用于抑制 router 的 analysis 桶因"怎么样/看看"等低权重口语词把纯闲聊误判为分析。
_ANALYSIS_SIGNAL_KW = (
    '分析', '解读', '洞察', '诊断', '评估', '探究', '研究', '相关性', '归因', '因果',
    'did', '双重差分', '回归', '建模', '训练', '预测', '清洗', '去重', '缺失值', '异常值',
    '画图', '可视化', '趋势', '数据', '文件', '数据集', '表格', '这张表', '这个表', '结果',
)
# 域外闲聊（非数据/金融/统计域）：本地礼貌回应，不联网、不硬答
_OUT_OF_SCOPE_KW = ('天气', '气温', '下雨', '笑话', '星座', '算命', '彩票', '开奖',
                    '几点', '星期几', '电影推荐', '菜谱', '怎么做菜')


def _has_analysis_signal(message: str) -> bool:
    """消息是否含真实的分析意图信号（分析动词/数据对象/明确标的/报告诉求）。"""
    if not message:
        return False
    low = message.lower()
    if any(k in low for k in _ANALYSIS_SIGNAL_KW):
        return True
    if _has_analyzable_target(message) or _wants_deep_report(message) or _needs_structured_research(message):
        return True
    return False


def _effective_action(message: str, router: dict, regex_action: str) -> str:
    """综合 router 与旧正则得到最终派发 action（P0-2）。

    优先级：router 高置信判定 > 旧正则。仅当 router 缺失/低置信且无法给出
    明确 action 时，才回退旧正则结果，保证行为可预期、不误伤。
    """
    if not router:
        return regex_action
    r_action = router.get('action')
    r_intent = router.get('intent')
    r_conf = float(router.get('confidence') or 0.0)
    if not r_action:
        return regex_action
    # 方法论/功能类问题：旧正则的 analyze_data 明显误判，一律采用 router 判定
    if regex_action == 'analyze_data' and (
            _is_methodology_question(message) or r_intent in ('operation', 'troubleshoot', 'knowledge')):
        return r_action if r_action in _KNOWN_ACTIONS else 'chat'
    # 防误伤（P1）：router 的 analysis 桶含"怎么样/看看"等低权重口语词，易把
    # 纯闲聊（"今天天气怎么样""嗯"）误判为分析。若消息没有任何真实分析信号，
    # 且旧正则也未判为分析 → 以旧正则为准，避免无谓落入"请上传文件"。
    if r_action == 'analyze_data' and regex_action != 'analyze_data' \
            and not _has_analysis_signal(message):
        return regex_action
    # router 置信度足够且 action 合法 → 采纳
    if r_conf >= 0.6 and r_action in _KNOWN_ACTIONS:
        return r_action
    # router 明确判定为非分析类（operation/knowledge/chat）→ 即便置信度略低也采纳，
    # 避免"怎么导出报告"被旧正则带进联网搜索
    if r_intent in ('operation', 'troubleshoot', 'knowledge') and r_action in _KNOWN_ACTIONS:
        return r_action
    return regex_action

# ==================== 股票标的解析（名称/代码 → 6 位代码） ====================

_STOCK_NAME_CACHE: Dict[str, str] = {}
_STOCK_CACHE_TS: float = 0.0


def get_stock_code_map() -> Dict[str, str]:
    """A 股「简称→代码」映射（缓存 24h）"""
    global _STOCK_NAME_CACHE, _STOCK_CACHE_TS
    if not _STOCK_NAME_CACHE or time.time() - _STOCK_CACHE_TS > 86400:
        try:
            from core_config import _get_akshare
            ak = _get_akshare()
            df = ak.stock_info_a_code_name()
            _STOCK_NAME_CACHE = dict(zip(df['name'].astype(str).str.strip(),
                                         df['code'].astype(str)))
            _STOCK_CACHE_TS = time.time()
        except Exception as e:
            logger.warning("A 股名单获取失败（沿用旧缓存）: %s", e)
    return _STOCK_NAME_CACHE


def resolve_stock_symbol(text: str) -> Optional[Dict[str, str]]:
    """从文本中解析股票标的：支持 6 位代码或中文简称。

    返回 {'code': '603986', 'name': '兆易创新', 'raw': 原文片段}；未命中返回 None。
    """
    if not text:
        return None
    # 1. 显式 6 位代码（如 600519 / 000001）
    m = re.search(r'(?<!\d)(\d{6})(?!\d)', text)
    names = get_stock_code_map()
    if m:
        code = m.group(1)
        nm = next((n for n, c in names.items() if c == code), '')
        return {'code': code, 'name': nm, 'raw': code}
    # 2. 中文简称（取最长匹配，避免短名误报）
    best = None
    for n, c in names.items():
        if len(n) >= 2 and n in text:
            if best is None or len(n) > len(best['name']):
                best = {'code': c, 'name': n}
    if best:
        best['raw'] = best['name']
        return best
    return None

# ==================== Data Context Provider ====================

class DataContextProvider:
    """Dynamically inject data context"""

    @staticmethod
    def get_current_data_context(file_name: str = None) -> str:
        """Get current data overview"""
        if not file_name:
            return ""

        try:
            from core_config import load_dataframe, OUT_DIR
            df = load_dataframe(str(OUT_DIR / file_name))
            if df is None or df.empty:
                return ""

            num_cols = df.select_dtypes(include='number').columns.tolist()
            cat_cols = df.select_dtypes(include='object').columns.tolist()

            parts = []
            parts.append(f"Data shape: {df.shape[0]} rows x {df.shape[1]} cols")
            if num_cols:
                parts.append(f"Numeric cols: {', '.join(num_cols[:8])}")
            if cat_cols:
                parts.append(f"Categorical cols: {', '.join(cat_cols[:5])}")

            return "\n".join(parts)
        except Exception:
            return ""

# ==================== Intent Recognizer ====================

class IntentRecognizer:
    """Rule-based intent recognizer"""

    PATTERNS = [
        # Chinese patterns (higher priority)
        # P2 收紧：原模式的数据名词为可选，导致"研究报告""数据分析方法"等被误捕为 analyze_data；
        # 现要求动作词后 10 字内出现数据/文件等**具体数据对象**（"分析当前文件"仍命中）。
        (r'(分析|查看|看看|研究|探索|检查).{0,10}(数据|文件|结果|数据集|当前文件|当前数据|这张表|这个表)', 'analyze_data', 0.9),
        (r'(帮我|我想|请|能|可以).{0,5}(分析|查看|看看|研究|检查|说说|介绍)', 'analyze_data', 0.85),
        (r'(当前|这个|本).{0,5}(数据|文件|表|数据集)', 'analyze_data', 0.85),
        (r'(数据|文件).{0,5}(分析|怎么样|如何|说什么|显示|包含)', 'analyze_data', 0.85),
        # English patterns
        (r'(analyze|analysis|explore|summarize|describe|examine|check|review).{0,10}(data|file|csv|dataset|current|this)?', 'analyze_data', 0.85),
        (r'(run|execute|write|generate).{0,10}(code|python|js|script|chart|visualization)', 'run_code', 0.9),
        (r'(draw|plot|create|generate).{0,10}(chart|graph|scatter|line|bar|pie|heatmap|histogram)', 'create_chart', 0.85),
        (r'(train|build|create).{0,10}(model|prediction|classification|regression|clustering)', 'train_model', 0.9),
        (r'(correlation|corr).{0,10}(analysis|matrix|coefficient)?', 'correlation', 0.9),
        (r'(clean|deduplicate|fill|missing|outlier).{0,10}(data)?', 'clean_data', 0.9),
        (r'(help|how|what|function|feature|guide|帮助|怎么用|功能|说明)', 'help', 0.95),
        (r'(hi|hello|hey|你好|嗨|哈喽)', 'greeting', 0.95),
    ]

    def recognize(self, text: str) -> Dict[str, Any]:
        text_lower = text.lower().strip()
        # P2：概念/方法论问法（"什么是DID""如何做好数据分析"）不应被 analyze 规则误捕为
        # 需要数据文件的分析请求；直接判为聊天/知识类，交由 intent_router 进一步判定。
        try:
            if _is_methodology_question(text):
                _p = self._extract_params(text_lower, 'chat')
                _p['text'] = text_lower
                return {
                    'action': 'chat',
                    'params': _p,
                    'confidence': 0.6,
                    'raw_text': text,
                }
        except Exception:
            pass
        for pattern, action, confidence in self.PATTERNS:
            if re.search(pattern, text_lower, re.IGNORECASE):
                return {
                    'action': action,
                    'params': self._extract_params(text_lower, action),
                    'confidence': confidence,
                    'raw_text': text
                }
        # 兜底分支也提取文件名：即便动作未命中（如"分析 sales.csv 文件"因中英混排
        # 未被动作正则捕获），也应把 file 参数带出，避免下游丢失数据对象。
        _p = self._extract_params(text_lower, 'chat')
        _p['text'] = text_lower
        return {
            'action': 'chat',
            'params': _p,
            'confidence': 0.5,
            'raw_text': text
        }

    def _extract_params(self, text: str, action: str) -> Dict[str, Any]:
        params = {}
        file_match = re.search(r'["\']?([^"\'\s]+\.(csv|xlsx|txt))["\']?', text)
        if file_match:
            params['file'] = file_match.group(1)
        return params

# ==================== Execution Plan ====================

@dataclass
class ExecutionStep:
    id: str
    action: str
    params: Dict
    description: str = ""

@dataclass
class ExecutionPlan:
    steps: List[ExecutionStep] = field(default_factory=list)

# ==================== Chat Manager ====================

@dataclass
class ChatMessage:
    role: str
    content: str
    timestamp: str = field(default_factory=lambda: datetime.now().strftime("%H:%M"))
    metadata: Dict = field(default_factory=dict)
    # 旧版本没有会话概念；加载旧数据时自动归入 legacy，避免升级后丢记录。
    conversation_id: str = 'legacy'
    # 秒级完整时间戳（2026-09-19）：本地归档按日期分文件夹需要"日期"，
    # 而 timestamp 只有 HH:MM 无法定位到哪一天。旧数据加载时自动补当天日期。
    ts: str = field(default_factory=lambda: datetime.now().isoformat(timespec='seconds'))

def _message_from_dict(m: Dict[str, Any]) -> Optional[ChatMessage]:
    """dict → ChatMessage（只取已知字段；老数据无 ts 用"当天+HH:MM"补，跨重启稳定）。

    只取 `_HISTORY_FIELDS`：旧数据缺字段能自动补默认值，未来新增字段也不会让
    加载整体崩掉（`ChatMessage(**m)` 遇未知键会 TypeError）。
    """
    d = {k: v for k, v in m.items() if k in _HISTORY_FIELDS}
    if not d.get('ts'):
        # 必须用"当天 + HH:MM"补，而不是 now()：ts 是归档条目 id 的来源，
        # 用 now() 会让同一条老记录每次重启都换一个 id，归档里越攒越多重复。
        stamp = str(d.get('timestamp') or '')
        d['ts'] = (f'{date.today().isoformat()}T{stamp}'
                   if stamp else datetime.now().isoformat(timespec='seconds'))
    return ChatMessage(**d)


class ChatManager:
    def __init__(self):
        self.recognizer = IntentRecognizer()
        self.history: List[ChatMessage] = self._load_history()
        self.context: Dict[str, Any] = {'last_file': None, 'last_action': None}
        # 多会话：每个会话独立上下文，避免 A 会话的 last_file 串到 B 会话
        self.active_conversation_id = 'legacy'
        self._conversation_contexts: Dict[str, Dict[str, Any]] = {
            'legacy': self.context,
        }
        # 历史读写/清空/删除共用的并发锁（clear/delete/get 均需用到）
        self._history_lock = threading.Lock()
        # P2-6：写盘节流状态 + 内存历史上限
        self._SAVE_MIN_INTERVAL = 2.0
        self._HISTORY_MEM_CAP = 400
        self._last_save_ts = 0.0
        self._history_dirty = False

    def _load_history(self) -> List[ChatMessage]:
        """从磁盘加载历史对话（重启不丢失，最多保留 100 条）"""
        try:
            if os.path.exists(HISTORY_PATH):
                with open(HISTORY_PATH, encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw, list):
                    out = []
                    for m in raw[-100:]:
                        if not isinstance(m, dict) or 'role' not in m or 'content' not in m:
                            continue
                        out.append(_message_from_dict(m))
                    return out
        except Exception as e:
            logger.warning('加载对话历史失败: %s', e)
        return []

    def _merge_disk_history(self):
        """把磁盘上"本进程内存里没有"的消息并回内存（防多进程互相覆盖）。

        背景（2026-09-19 实测）：`data/chat_history.json` 是**单文件全量回写**，
        而任何 `import ai_assistant` 的东西（诊断脚本、验证脚本、批处理工具）都会
        各自持有一份内存副本并在 atexit 回写。谁最后退出谁说了算 —— 实测跑一次
        探针脚本，就把服务进程在此期间新增的整段对话抹掉了。
        写盘前先读盘合并，把"别的进程写进去、我没见过"的消息捡回来。
        """
        try:
            if not os.path.exists(HISTORY_PATH):
                return
            with open(HISTORY_PATH, encoding='utf-8') as f:
                raw = json.load(f)
        except Exception:
            return
        if not isinstance(raw, list):
            return
        try:
            seen = {(m.role, m.content, getattr(m, 'ts', '') or '') for m in self.history}
            added = 0
            for m in raw:
                if not isinstance(m, dict) or 'role' not in m or 'content' not in m:
                    continue
                key = (m.get('role'), m.get('content'), m.get('ts') or '')
                if key in seen:
                    continue
                seen.add(key)
                try:
                    self.history.append(_message_from_dict(m))
                    added += 1
                except Exception:
                    continue
            if added:
                # 按 ts 稳定排序（stable：同秒的问答保持原有相对次序）
                self.history.sort(key=lambda x: getattr(x, 'ts', '') or '')
                self._trim_history_memory()
                logger.info('合并磁盘历史增量 %d 条（检测到其它进程写入）', added)
        except Exception as e:
            logger.debug('合并磁盘历史失败（忽略）: %s', e)

    def _save_history(self, force: bool = False, merge_disk: bool = True):
        """持久化历史对话（P2-6：写盘节流，避免每条消息全量重写 100 条 JSON）。

        内存中最多保留 _HISTORY_MEM_CAP 条，落盘仍取最近 100 条（与原契约一致）。
        节流窗口内的增量由 flush_history()（进程退出/显式调用）补写。

        merge_disk=False 用于**删除类**操作：删除必须"以内存为准"。否则
        _merge_disk_history 会把刚删掉、但磁盘上还在的消息当成"别的进程写入的新增"
        再捡回来 —— 实测 delete-message 报了 removed=1 却一条都没删掉。
        """
        try:
            import time as _t
            now = _t.time()
            if not force and (now - getattr(self, '_last_save_ts', 0.0)) < self._SAVE_MIN_INTERVAL:
                self._history_dirty = True
                return
            self._last_save_ts = now
            self._history_dirty = False
            os.makedirs(os.path.dirname(HISTORY_PATH), exist_ok=True)
            # 写前先合并磁盘增量：脚本/诊断工具与服务共享同一份 json，
            # 各自 atexit 全量回写会互相覆盖（实测丢过整段对话）。
            # 删除操作传 merge_disk=False —— 见该参数说明。
            if merge_disk:
                self._merge_disk_history()
            with open(HISTORY_PATH, 'w', encoding='utf-8') as f:
                json.dump([{'role': m.role, 'content': m.content, 'timestamp': m.timestamp,
                            'metadata': m.metadata,
                            'conversation_id': getattr(m, 'conversation_id', 'legacy'),
                            # ts 必须落盘：否则重启后补"今天"，同一条消息会在归档里
                            # 按两个日期各存一份（归档 id 由 ts 派生）
                            'ts': getattr(m, 'ts', '')}
                           for m in self.history[-100:]], f, ensure_ascii=False)
        except Exception as e:
            logger.warning('保存对话历史失败: %s', e)
        # 顺带把"全量"历史合并进本地归档（主存只留 100 条，归档不截断）
        _sync_chat_archive()

    def flush_history(self, merge_disk: bool = True):
        """立即落盘（供进程退出/切换会话等关键点调用）。

        merge_disk=False：删除/清空专用（避免刚删的记录被 merge 从磁盘捡回来）。
        """
        self._save_history(force=True, merge_disk=merge_disk)

    def _trim_history_memory(self):
        """内存历史截断（P2-6：原实现 history 无上限增长）"""
        try:
            if len(self.history) > self._HISTORY_MEM_CAP:
                del self.history[:len(self.history) - self._HISTORY_MEM_CAP]
        except Exception:
            pass

    def process_message(self, user_message: str, llm_config: dict = None, current_file: str = None) -> Dict[str, Any]:
        self.history.append(ChatMessage(role='user', content=user_message,
                                        conversation_id=getattr(self, 'active_conversation_id', 'legacy')))
        self._trim_history_memory()
        self._save_history()
        if current_file:
            self.context['last_file'] = current_file

        intent = self.recognizer.recognize(user_message)
        action = intent['action']
        params = intent.get('params', {})

        # 仅当消息明确引用上下文文件（当前/这个/上次/对比...）时才补历史文件，
        # 避免"什么是数据分析"这类问题被自动补上文件去执行分析
        _ctx_ref = ('当前', '这个', '本文件', '本次', '上次', '之前', '刚才', '继续', '对比', '上回', '那个')
        if 'file' not in params and self.context.get('last_file') and any(w in user_message for w in _ctx_ref):
            params['file'] = self.context['last_file']

        self.context['last_action'] = action

        file_name = params.get('file', '')
        steps = []

        if action == 'analyze_data':
            if file_name:
                steps = [
                    ExecutionStep(id='s1', action='switch_tab', params={'tab': 'tab-analyze'}, description='打开数据分析页'),
                    ExecutionStep(id='s2', action='call_api', params={'url': '/api/describe', 'method': 'POST', 'body': {'file': file_name}}, description=f'描述统计 {file_name}'),
                    ExecutionStep(id='s3', action='call_api', params={'url': '/api/correlation', 'method': 'POST', 'body': {'file': file_name}}, description='相关性分析（识别关键驱动因素）'),
                    ExecutionStep(id='s4', action='save_snapshot', params={'file': file_name}, description='保存数据快照'),
                    ExecutionStep(id='s5', action='autonomous_analysis', params={'file': file_name, 'message': user_message}, description='AI 深度分析（自动调用 DID/趋势/分组等工具）'),
                ]
            else:
                # 未选文件：尝试从消息中解析股票标的，自动获取行情数据后再分析
                sym = resolve_stock_symbol(user_message)
                if sym:
                    label = (sym.get('name') + '(' + sym.get('code') + ')') if sym.get('name') else sym.get('code')
                    steps = [
                        ExecutionStep(id='s1', action='call_api', params={'url': '/api/finance/fetch-stock', 'method': 'POST', 'body': {'symbol': sym['raw']}}, description=f'自动获取 {label} 日K行情'),
                        ExecutionStep(id='s2', action='call_api', params={'url': '/api/describe', 'method': 'POST', 'body': {'file': '{{lastFile}}'}}, description='描述统计行情数据'),
                        ExecutionStep(id='s3', action='call_api', params={'url': '/api/chart/custom', 'method': 'POST', 'body': {'file': '{{lastFile}}', 'x': [], 'y': ['close'], 'type': 'line'}}, description=f'{label} 股价走势图'),
                        ExecutionStep(id='s4', action='autonomous_analysis', params={'file': '{{lastFile}}', 'message': user_message}, description='AI 深度分析（走势/估值判断）'),
                    ]
                else:
                    # 未选文件也无标的：给出可执行引导，而不是空计划
                    steps = [
                        ExecutionStep(id='s1', action='show_message',
                                      params={'content': '请先在「数据管理」页上传或选择一个数据文件，'
                                                          '然后告诉我"分析当前数据"即可开始。'},
                                      description='引导用户选择数据文件'),
                    ]
        elif action == 'help':
            steps = [
                ExecutionStep(id='s1', action='show_message', params={'content': 'Functions: analyze, clean, model, chart. Say "analyze [filename]" to start.'}, description='Help'),
            ]
        elif action == 'greeting':
            steps = [
                ExecutionStep(id='s1', action='show_message', params={'content': 'Hello! I am your data analysis assistant. Say "analyze [filename]" to begin.'}, description='Greeting'),
            ]
        else:
            # 兜底：给用户的友好引导，绝不显示内部调试信息（Intent/Params 原文）
            steps = [
                ExecutionStep(id='s1', action='show_message',
                              params={'content': '我还没完全理解你的意思。可以试试：\n'
                                                 '• 上传数据后说"帮我分析当前文件"\n'
                                                 '• 直接问股票/基金，如"分析兆易创新"\n'
                                                 '• 或问功能用法，如"怎么导出报告"'},
                              description='引导'),
            ]

        return {
            'ok': True,
            'intent': intent,
            'plan': {'steps': [{'id': s.id, 'action': s.action, 'params': s.params, 'description': s.description} for s in steps], 'context': self.context},
            'source': 'rules'
        }

# ==================== Global Instance ====================

_chat_manager = ChatManager()


def _sync_chat_archive(messages=None) -> dict:
    """把内存历史全量合并进本地归档文件夹（data/chat_history/YYYY-MM-DD/）。

    与主存 data/chat_history.json 的区别：主存只留最近 100 条（接口用），
    归档**合并写入、不截断**，是"所有聊天记录保留在本地"的落点。
    归档失败一律静默 —— 绝不能因为磁盘/权限问题让对话流程报错。
    """
    try:
        import chat_archive
        items = list(_chat_manager.history) if messages is None else list(messages)
        return chat_archive.sync_messages(items)
    except (Exception, SystemExit) as e:
        logger.debug('聊天归档写入失败（忽略）: %s', e)
        return {'ok': False, 'error': str(e)}


# P2-6：进程退出时补写节流窗口内的历史增量
import atexit as _atexit


def _flush_chat_history_at_exit():
    try:
        _chat_manager.flush_history()
    except Exception:
        pass


_atexit.register(_flush_chat_history_at_exit)

# ==================== 金融实时分析（估值/股价类优先取真实数据） ====================

_STOCK_ALIASES = {
    '腾讯': 'HK0700', '阿里巴巴': 'HK9988', '美团': 'HK03690', '小米': 'HK01810',
    '京东': 'HK09618', '网易': 'HK09999', '百度': 'HK09888', '快手': 'HK01024',
    '贵州茅台': '600519', '茅台': '600519', '五粮液': '000858', '泸州老窖': '000568',
    '山西汾酒': '600809', '宁德时代': '300750', '隆基绿能': '601012', '中国平安': '601318',
    '招商银行': '600036', '万科': '000002', '恒瑞医药': '600276', '药明康德': '603259',
    '中芯国际': '688981', '海康威视': '002415', '立讯精密': '002475', '东方财富': '300059',
    '同花顺': '300033', '工业富联': '601138', '比亚迪': '002594', '牧原股份': '002714',
    # 兆易创新及更多常见标的（半导体/芯片/消费电子/新能源/金融）
    '兆易创新': '603986', '兆易': '603986', '汇顶科技': '603160', '韦尔股份': '603501',
    '澜起科技': '688008', '北京君正': '300223', '北方华创': '002371', '中微公司': '688012',
    '华虹公司': '688347', '华虹半导体': '688347', '长电科技': '600584', '通富微电': '002156',
    '三安光电': '600703', '紫光国微': '002049', '圣邦股份': '300661', '士兰微': '600460',
    '卓胜微': '300782', '闻泰科技': '600745', '澜起': '688008', '兆易创新科技': '603986',
    '美的集团': '000333', '海尔智家': '600690', '格力电器': '000651', '海天味业': '603288',
    '伊利股份': '600887', '恒瑞医药科技': '600276', '中兴通讯': '000063', '中国中免': '601888',
    '隆基股份': '601012', '通威股份': '600438', '阳光电源': '300274', '比亚迪股份': '002594',
    '寒武纪': '688256', '海光信息': '688041', '龙芯中科': '688047', '中科曙光': '603019',
    '科大讯飞': '002230', '金山办公': '688111', '三六零': '601360', '中国软件': '600536',
    '中国石油': '601857', '中国石化': '600028', '工商银行': '601398', '建设银行': '601939',
    '农业银行': '601288', '中国银行': '601988', '国电南瑞': '600406', '中国移动': '600941',
    '海康威视科技': '002415', '大华股份': '002236', '京东方A': '000725', '京东方': '000725',
    'TCL科技': '000100', 'TCL中环': '002129', '韦尔股份科技': '603501',
    '星宇股份': '601799', '星宇': '601799',   # 星宇股份（车灯，601799）
}
# 财务报表类意图词（配合 _has_analyzable_target 让"分析 XX 财务报表"无需硬编码代码也能被识别）
_FIN_STATEMENT_KW = ('财务报表', '财报', '年报', '季报', '半年报', '三大报表', '财务数据',
                     '资产负债表', '利润表', '现金流量表', '净资产', '净利润')
_FIN_INTENT_KW = ('股价', '估值', '市盈率', '市净率', 'PE', 'PB', 'DCF', '合理', '值得买',
                  '低估', '高估', '贵不贵', '贵吗', '便宜', '买入', '目标价', '上涨空间') + _FIN_STATEMENT_KW
# 动作词：带"分析/看看/研究"等动作 + 能提取出股票代码 → 也视为金融分析意图
_FIN_ACTION_KW = ('分析', '看看', '了解', '研究', '查一下', '查查', '讲讲', '评价',
                  '怎么样', '如何', '帮我看', '帮我分析', '看下', '看一下', '说说', '点评')


def _extract_stock_code(message: str):
    """从消息提取股票代码（HK+4位 / 6位A股 / 公司名）"""
    import re
    m = re.search(r'HK\s?(\d{4})', message, re.I)
    if m:
        return 'HK' + m.group(1)
    m = re.search(r'\b(\d{6})\b', message)
    if m:
        return m.group(1)
    for name, code in _STOCK_ALIASES.items():
        if name in message:
            return code
    return None


# 金融实时取数短缓存：code -> {ts, ctx}（60s TTL，指代追问秒回；深度分析强制绕过）
_FIN_LIVE_CACHE: dict = {}
# P1-6：金融实时取数总超时（5 类接口并行跑，超时后已完成的照常采用）
_FIN_LIVE_TOTAL_TIMEOUT = 10.0
_AI_HEARTBEAT_S = int(os.environ.get('AI_HEARTBEAT_S', '15'))
# P1-4③：流式深度分析的"卡死看门狗"。旧实现 agent 线程一旦卡死，前端只会无限收到
# ping 而永远转圈、永不报错。超过该时长无任何进展事件即判定卡死并主动收尾。
_AI_STALL_LIMIT_S = float(os.environ.get('AI_STREAM_STALL_LIMIT', '300'))


def _finance_live_context(message: str, code: str = None, use_cache: bool = True):
    """金融估值/股价类问题：自动调用金融数据接口，返回实时数据文本；否则 None。

    成功时把标的记入会话上下文（last_stock），供"对当前数据深入分析"这类
    指代式追问复用——上一轮的真实取数结果不丢失。
    use_cache=True 时命中 60s 内同标的的取数结果直接返回（指代追问秒回）；
    深度分析路径传 use_cache=False 强制取最新。"""
    # ---- 60s 短缓存：快速追问（"股价""那估值呢"）避免重复拉取 akshare ----
    fin_code_probe = code or _extract_stock_code(message)
    if use_cache and fin_code_probe:
        cached = _FIN_LIVE_CACHE.get(fin_code_probe)
        if cached and time.time() - cached['ts'] < 60:
            cached['ctx']['code'] = fin_code_probe
            return cached['ctx']
    if not code:
        hit_kw = any(k.lower() in message.lower() for k in _FIN_INTENT_KW)
        hit_action = any(k in message for k in _FIN_ACTION_KW)
        if not hit_kw and not hit_action:
            return None
        code = _extract_stock_code(message)
        if not code:
            return None
    try:
        # P1-6：5 类取数（DCF/财务比率/公司档案/技术面/可比估值）改为线程池并行，
        # 总超时兜底——原实现串行阻塞，网络差时用户卡数十秒
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _f_dcf():
            from valuation import dcf_valuation
            r = dcf_valuation(code, {})
            if not r.get('ok'):
                return None
            return (f"DCF 估值({code} {r.get('name', '')}): 现价={r.get('price')}元, 每股内在价值={r.get('per_share')}元, "
                    f"偏差={r.get('spread_pct')}%, 结论={r.get('verdict_cn')}"
                    f"（盈利基数{r.get('base_fcf_yi')}亿, 高增长率{r.get('g1')}%, 折现率{r.get('r')}%, 永续增长{r.get('g2')}%）")

        def _f_ratios():
            from financial_statement import fetch_financial_data, calculate_ratios
            fd = fetch_financial_data(code)
            if not fd.get('ok'):
                return None
            rr = calculate_ratios(fd)
            if not rr.get('ok'):
                return None
            ra = rr.get('ratios', {})
            p, s, g = ra.get('profitability', {}), ra.get('solvency', {}), ra.get('growth', {})
            items = [f"ROE={p.get('roe')}%", f"净利率={p.get('net_margin')}%", f"毛利率={p.get('gross_margin')}%",
                     f"负债率={s.get('debt_ratio')}%", f"营收同比={g.get('revenue_growth')}%",
                     f"净利同比={g.get('profit_growth')}%"]
            return f"财务比率({code}): " + ", ".join(x for x in items if x)

        def _f_profile():
            # 公司综合档案：行业/主营/市值/上市日期等（基本面全景，非仅财务；东财接口偶发断连则跳过）
            from financial_statement import _resolve_stock_name as _rsname
            import akshare as _pk
            _pinfo = None
            try:
                _pinfo = _pk.stock_individual_info_em(symbol=code)
            except Exception:
                _pinfo = None
            _seg = []
            if isinstance(_pinfo, dict) and _pinfo:
                _nm = _pinfo.get("股票简称") or _pinfo.get("股票名称")
                _ind = _pinfo.get("行业")
                _ls = _pinfo.get("上市时间")
                if _nm: _seg.append(f"简称={_nm}")
                if _ind: _seg.append(f"行业={_ind}")
                for _mkv, _lab in (('总市值(元)', '总市值'), ('流通市值(元)', '流通市值')):
                    if _pinfo.get(_mkv) is not None:
                        try:
                            _seg.append(f"{_lab}={float(_pinfo[_mkv])/1e8:.1f}亿")
                        except (ValueError, TypeError):
                            _seg.append(f"{_lab}={_pinfo[_mkv]}")
                if _ls: _seg.append(f"上市时间={_ls}")
            # 兜底名称（东财接口失败时用新浪报表接口解析）
            if not any('简称' in s for s in _seg):
                _fallback_nm = _rsname('a', code, ('sh' if code.startswith('6') else 'sz') + code)
                if _fallback_nm:
                    _seg.insert(0, f"简称={_fallback_nm}")
            return (f"公司档案({code}): " + ", ".join(_seg)) if _seg else None

        def _f_tech():
            # 技术面：近期股价动量（近5日/20日/60日涨跌幅 + 最新收盘），K线真实数据
            import akshare as _tk
            _mk = ('a' if code.isdigit() and not code.startswith('HK') else 'hk')
            _sina = (('sh' if code.startswith('6') else 'sz') + code) if _mk == 'a' else ''
            if _mk == 'a':
                _df = _tk.stock_zh_a_daily(symbol=_sina, adjust="qfq")
            else:
                _df = _tk.stock_hk_daily(symbol=code, adjust="qfq")
            if _df is None or len(_df) < 2:
                return None
            _cl = _df['close']
            _last, _prev = float(_cl.iloc[-1]), float(_cl.iloc[-2])
            def _chg(_n):
                if len(_cl) <= _n:
                    return None
                _base = float(_cl.iloc[-1 - _n])
                return round((_last - _base) / _base * 100, 2)
            _c5, _c20, _c60 = _chg(5), _chg(21), _chg(61)
            _seg = [f"最新收盘={_last}元"]
            if _c5 is not None: _seg.append(f"近5日={_c5:+.2f}%")
            if _c20 is not None: _seg.append(f"近月={_c20:+.2f}%")
            if _c60 is not None: _seg.append(f"近季={_c60:+.2f}%")
            _seg.append(f"较昨{('涨' if _last >= _prev else '跌')}{abs(_last - _prev):.2f}元")
            # 均线多头/空头
            _ma20 = float(_cl.iloc[-20:].mean())
            return f"技术面({code}): " + ", ".join(_seg) + f"，MA20={'价位上方' if _last >= _ma20 else '价位下方'}"

        def _f_peers():
            # 相对估值：可比公司 PE/PB 中位数对比（巴菲特框架需多模型估值支撑）
            from valuation import peer_comparison
            # 内置可比公司映射：常见核心标的 → 同行业可比代码
            _PEERS_BY_CODE = {
                '600519': ['000858', '000568', '600809'],  # 茅台 → 五粮液/泸州老窖/山西汾酒
                '000858': ['600519', '000568', '600809'],
                '300750': ['002594', '601127'],            # 宁德 → 比亚迪/赛力斯
                '600036': ['601398', '601939', '000001'],  # 招行 → 工行/建行/平安
            }
            _peers = _PEERS_BY_CODE.get(code)
            pc = peer_comparison(code, peers=_peers) if _peers else None
            if not (isinstance(pc, dict) and not pc.get('error')):
                return None
            rows = pc.get('rows') or []
            med = pc.get('median') or {}
            verdict = pc.get('verdict') or {}
            out = []
            if rows:
                items = []
                for row in rows[:6]:
                    nm = row.get('name') or row.get('code') or ''
                    pe, pb = row.get('pe'), row.get('pb')
                    items.append(f"{nm}(PE={pe if pe is not None else '-'},PB={pb if pb is not None else '-'})")
                out.append("可比公司PE/PB相对估值: " + "; ".join(x for x in items[:6]))
            if med:
                out.append(f"可比中位数: PE={med.get('pe')}, PB={med.get('pb')}")
            if verdict and verdict.get('label'):
                out.append(f"相对估值结论: {verdict['label']}")
            return "\n".join(out) if out else None

        tasks = [(_f_dcf, 0), (_f_ratios, 1), (_f_profile, 2), (_f_tech, 3), (_f_peers, 4)]
        results = [None] * len(tasks)
        pool = ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix='finlive')
        try:
            futs = {pool.submit(fn): idx for fn, idx in tasks}
            try:
                for fut in as_completed(futs, timeout=_FIN_LIVE_TOTAL_TIMEOUT):
                    idx = futs[fut]
                    try:
                        results[idx] = fut.result()
                    except Exception:
                        results[idx] = None
            except Exception:
                pass  # 总超时（或调度异常）：已完成的结果照常采用，未完成的放弃等待
        finally:
            # 不等待剩余线程（wait=False），超时未完成的任务不再阻塞主请求
            try:
                pool.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                pool.shutdown(wait=False)

        parts = [r for r in results if r]
        if not parts:
            return None
        parts.append("免责声明: 本结论为客观数据分析，仅供参考，不构成任何投资建议。")
        # 记入会话上下文：供指代式追问（"对当前数据深入分析"）复用真实取数标的
        try:
            _chat_manager.context['last_stock'] = {'code': code, 'ts': time.time()}
            _FIN_LIVE_CACHE[code] = {'ts': time.time(), 'ctx': {'code': code, 'context': "\n".join(parts)}}
        except Exception:
            pass
        return {'code': code, 'context': "\n".join(parts)}
    except Exception:
        return None


def _skills_context() -> str:
    """已启用 Skills 的提示块（普通对话也注入，让导入的 skill 真正生效）"""
    try:
        from skill_manager import get_skills_prompt
        return get_skills_prompt()
    except Exception:
        return ''


def _router_context(message: str, llm_config: dict = None, current_file: str = None):
    """意图分流前导（同步/流式共享）：路由 + 规范化 + 角色提示 + 金融子意图角色增强 + 记忆写入。

    消除同步、流式两条链路各自实现导致的角色/记忆漂移。
    返回 (intent_router, norm_msg, role_prompt)。"""
    # 意图分流中间层（路由器）：分类 + 同义词归一 + 角色/深度（先于知识库/Agent 命中）
    _intent_router = _route_intent(message, llm_config)
    _norm_msg = _intent_router.get('normalized') or message
    _role_prompt = _intent_router.get('role_prompt') or ''
    # 金融子意图增强：命中金融子领域 → 追加对应专家角色与风险护栏
    _fin_meta = _intent_router.get('finance') or {}
    if _fin_meta.get('is_finance'):
        _fin_extra = _finance_role_for_subintent(_fin_meta.get('subintent'))
        if _fin_extra:
            _role_prompt = (_fin_extra + "\n" + _role_prompt) if _role_prompt else _fin_extra
    # 记忆：记录结构化路由元数据（意图/实体/文件），供长期偏好上下文复用
    try:
        from conversation_memory import get_memory
        get_memory().add_turn(message, _intent_router, current_file)
    except Exception:
        pass
    return _intent_router, _norm_msg, _role_prompt


def _joined_kb_hits(message: str, norm_msg: str, fin_meta: dict):
    """知识库检索 + 合并去重 + 金融子意图抑制（同步/流式共享决策）。

    返回 (merged, is_kb, ent_is_kb)。检索/抑制逻辑集中在单点，避免双路径漂移。"""
    try:
        from knowledge_rag import search_knowledge, is_knowledge_query, ACTION_WORDS
        kb_hits = search_knowledge(norm_msg, top_k=3)
        ent_scored = _search_enterprise_knowledge(norm_msg, top_k=3)
        ent_hits = [e for _, e in ent_scored]
        # 合并去重（知识点库优先，企业知识库补充）
        merged = list(kb_hits)
        seen = {h.get('name') for h in kb_hits}
        for h in ent_hits:
            if h.get('name') not in seen:
                seen.add(h['name'])
                merged.append(h)
        is_kb = is_knowledge_query(message, kb_hits)
        # 抑制：金融估值/行情子意图命中时 → 不走知识回答，留给金融分析工具/Agent 处理，
        # 避免单一名称单字命中无关知识库词条；概念/方法问法（什么是X / 怎么算）即使命中也不抑制
        _fin_sub = (fin_meta or {}).get('subintent')
        _is_finance_analysis = bool(_fin_sub and _fin_sub in (
            'stock_valuation', 'market_quote', 'fund_performance', 'risk_assessment'))
        _is_concept_ask = any(w in message for w in (
            '什么是', '是什么', '指什么', '啥是', '怎么算', '如何算', '怎么计算', '如何计算',
            '含义', '概念', '定义', '是什么意思', '什么意思', '区别', '对比', '公式', '怎么理解'))
        if _is_finance_analysis and not _is_concept_ask:
            # 已命中明确估值/行情请求：不抢，留给估值分析流程
            ent_is_kb = False
        else:
            # 企业库命中也走知识回答，但分析类意图（分析/训练/预测...）不抢，留给数据分析流程
            ent_is_kb = bool(ent_hits) and _looks_like_question(message) and not any(w in message for w in ACTION_WORDS)
        return merged, is_kb, ent_is_kb
    except Exception:
        return [], False, False


def _chat_guard_reply(message: str, action: str, intent_router: dict, result: dict, current_file: str):
    """聊天语向内全部本地兜底回复（问候/帮助/域外/计算/乱码）。

    命中返回可直接作为 reply 的 payload dict；否则返回 None（交由调用方走 LLM 回答）。
    逻辑集中在单点，消除同步/流式双路径的兜底漂移。"""
    _intent = intent_router.get('intent')
    # ---- 问候语：本地友好回复（绝不联网 / 不调 LLM）----
    if action == 'greeting':
        return {
            'ok': True,
            'reply': '你好呀！👋 我是你的数据助手，有什么数据或金融分析需要帮忙的吗？',
            'source': 'fast',
            'intent': result.get('intent'),
            'plan': {'steps': [], 'context': self_context_summary()},
        }
    # ---- 帮助请求：本地返回功能清单（无需联网）----
    if (action == 'help' or _intent in ('operation', 'troubleshoot')
            or any(w in message for w in ('怎么用', '功能有哪些', '能做什么', '帮助', '怎么操作', '怎么用这个'))):
        return {
            'ok': True,
            'reply': '我可以做这些事：\n\n'
                     '📊 **数据分析**：顶部选文件后说"帮我分析当前文件"\n'
                     '🏦 **金融分析**：直接问股票/基金（自动取真实数据）\n'
                     '🧹 **数据清洗** / 📈 **画图** / 🔗 **相关性** / 🤖 **机器学习** / 🎯 **DID 因果**\n'
                     '📚 **知识问答**：金融、税务、统计学概念\n'
                     '🧭 **报告导出**：分析完成后点"导出报告"即可保存 Word/Excel/PDF\n\n'
                     '🧠 **思考模式**：联网查最新资料深度回答；⚡ **快速模式**：秒回\n\n'
                     '试试对我说："帮我分析当前文件" 或 "什么是夏普比率"。',
            'source': 'fast',
            'intent': result.get('intent'),
            'plan': {'steps': [], 'context': self_context_summary()},
        }
    # ---- 域外闲聊（非数据域）：本地礼貌回应，不联网搜索 ----
    # P0-4：除本地关键词表外，也接受路由层的 out_of_scope 判定
    # （路由层词表更全：翻译/写作文/娱乐/时间类等），避免落入 LLM 硬答。
    if _intent == 'out_of_scope' or any(w in message for w in _OUT_OF_SCOPE_KW):
        return {
            'ok': True,
            'reply': '这个我帮不上忙 😅 我是数据与金融分析助手，主要能力包括：\n'
                     '📊 **数据分析**（选文件后说"帮我分析当前文件"）\n'
                     '🏦 **金融分析**（直接问股票/基金，如"分析兆易创新股价合理吗"）\n'
                     '📚 **知识问答**（如"什么是夏普比率"）\n'
                     '试试上面这些，我就能帮到你了。',
            'source': 'fast',
            'intent': {'action': 'chat', 'params': {}, 'confidence': 0.9, 'raw_text': message},
            'plan': {'steps': [], 'context': self_context_summary()},
        }
    # ---- 自然语言查数（nlq）：真值直出优先，无数据载体则明确引导 ----
    # 【2026-09-17 修复 P0】nlq 此前在 sync（chat_actions）与 stream（第 27xx 行元组）
    # 两条链路都**没有分派分支**，会直接穿透到函数末尾 `return result`，而 result 对 nlq
    # 不产生 reply → 用户拿到**空回复**（实测"帮我看看收益率""前五名是谁"reply_len=0，
    # 且未调用 LLM）。现统一在本共享守卫里兜底（sync/stream 自动共享，消除双轨漂移）：
    #   有文件 + 后端能算出真值 → 直接给真值（最准、零幻觉）；
    #   有文件 + 无专用计算器   → 返回 None，交上层注入「真实数据摘要」后由 LLM 解读；
    #   无文件                  → 明确引导（绝不空回、绝不编造数字）。
    if action == 'nlq':
        if current_file:
            try:
                from real_compute import compute_and_render
                _nr = compute_and_render(message, current_file)
                if _nr and _nr.get('text'):
                    return {
                        'ok': True, 'reply': _nr['text'],
                        'source': 'computation', 'intent': result.get('intent'),
                        'plan': {'steps': [], 'context': self_context_summary()},
                    }
            except Exception:
                pass  # 计算失败 → 交上层注入数据摘要后回答
        else:
            return {
                'ok': True,
                'reply': '🔢 我来帮你查数。不过目前还没有可查的数据，请先给我一个查询对象：\n\n'
                         '1. **有数据文件**：在顶部选择文件（.csv / .xlsx），然后直接问，\n'
                         '   例如 **"帮我算一下销售额总和"**、**"前五名是谁"**、**"有多少条记录"**；\n'
                         '2. **问金融标的**：直接说标的即可，例如 **"茅台的收益率是多少"**、\n'
                         '   **"沪深 300 最近一年涨了多少"**，我会联网取真实数据来算。',
                'source': 'rules',
                'intent': result.get('intent'),
                'plan': {'steps': [], 'context': self_context_summary()},
            }
    # ---- 计算类请求：本地真实计算优先（不联网搜索算式）----
    if detect_special_intent_safe(message) == 'computation':
        try:
            from real_compute import compute_and_render
            _cr = compute_and_render(message, current_file)
            if _cr and (_cr.get('block') or _cr.get('text') or _cr.get('answer')):
                return {
                    'ok': True,
                    'reply': _cr.get('text') or _cr.get('answer') or _cr.get('block'),
                    'source': 'computation', 'intent': result.get('intent'),
                    'plan': {'steps': [], 'context': self_context_summary()},
                }
        except Exception:
            pass  # 本地计算失败则走常规回答
    # ---- 乱码/无意义输入：本地引导，不浪费联网搜索 ----
    _has_cjk = any('\u4e00' <= ch <= '\u9fff' for ch in message)
    _has_sense_en = bool(re.search(
        r'\b(what|how|why|who|when|where|help|hi|hello|hey|yes|no|ok|thanks|thank|'
        r'please|sum|mean|avg|average|analy|analysis|data|file|csv|chart|plot|stock|fund|'
        r'test|list|show|tell|explain|compare|is|are|the|a\b)\b', message, re.IGNORECASE))
    # 疑似乱码：无中文 + 无有意义英文词（无空格长串或纯符号）
    _junk = (not _has_cjk) and not _has_sense_en and (
        ' ' not in message.strip() or len(message.strip()) <= 3)
    if _junk or len(message.strip()) <= 1:
        return {
            'ok': True,
            'reply': '我没有理解这句话 😅 可以试试：\n'
                     '• 上传数据后说 **"帮我分析当前文件"**\n'
                     '• 直接问 **"分析兆易创新的股价合理吗"**\n'
                     '• 或问 **"什么是夏普比率"**',
            'source': 'fast',
            'intent': result.get('intent'),
            'plan': {'steps': [], 'context': self_context_summary()},
        }
    return None


def process_chat_message(message: str, llm_config: dict = None, current_file: str = None,
                         mode: str = 'think', images: list = None,
                         conversation_id: str = None) -> Dict[str, Any]:
    """
    处理对话消息。
    mode:
      - 'fast' : 快速模式 —— 知识问答直接返回原文（不调 LLM，秒回）；聊天不联网，LLM 简短回答。
      - 'think': 思考模式 —— 知识问答 LLM 综合归纳；聊天联网搜索 + LLM；时效性问题优先联网。
    conversation_id: 多会话隔离键；非空时入口即切换当前会话
                     （route cache / 记忆桶 / last_file 一起跟着切，否则串台）。
    """
    fast = mode == 'fast'
    if conversation_id:
        activate_chat_conversation(conversation_id)

    # 纯寒暄秒回：不进路由/知识库/专家约束/LLM（压思考时延，2026-09-22）
    if _is_pure_greeting(message):
        _g = _greeting_payload()
        _g['intent']['raw_text'] = message
        return _g

    # 识图（多模态）：带图片优先走视觉模型
    if images:
        _blocked = _vision_egress_blocked(message)
        if _blocked:
            return _blocked
        content = [{"type": "text", "text": message or "请描述这张图片的内容"}]
        for img in images[:4]:
            content.append({"type": "image_url", "image_url": {"url": img}})
        v_msgs = [
            {"role": "system", "content": "你是 data.tool 的多模态分析助手，具备图片识别能力。请根据用户要求描述或分析图片内容，用中文回答，简洁准确。"},
            {"role": "user", "content": content},
        ]
        try:
            from llm_client import chat
            resp = chat(v_msgs, llm_config, max_tokens=1000)
            ans = (resp.get('content', '') if isinstance(resp, dict) else str(resp)).strip()
            if ans:
                return {
                    'reply': ans, 'source': 'vision', 'llm_synthesized': True,
                    'intent': {'action': 'chat', 'params': {}, 'confidence': 0.9, 'raw_text': message},
                }
        except Exception:
            pass
        return {
            'reply': '⚠️ 当前模型不支持图片识别（识图需要支持视觉的多模态模型）。请在 AI 助手设置中切换支持视觉的模型后重试。',
            'source': 'vision', 'llm_synthesized': False,
            'intent': {'action': 'chat', 'params': {}, 'confidence': 0.9, 'raw_text': message},
        }

    _time_words = ('最新', '现在', '目前', '当前', '今天', '今年', '最近', '实时', '今日', '当前值')
    _fin_tax_words = ('利率', '汇率', '税', '房贷', '存款', '贷款', '股票', '基金', '指数', '行情', '点位', '价格')
    force_web = (not fast) and any(w in message for w in _time_words) and any(w in message for w in _fin_tax_words)

    # 金融估值/股价类问题 → 自动调用金融数据接口取真实数据（优先于知识库通用回答）
    # 注意：思考模式下的深度分析请求（"深入分析茅台值不值得买"）不在本轮短路，
    # 放行到下方 Agent 链路做全维度结构化报告（金融实时数据会在 Agent 内再次注入），
    # 否则深度问题永远只能拿到 200 字快答。
    _fin_live = _finance_live_context(message)
    if not _fin_live and _references_recent_target(message):
        # 指代式追问（"那它现在估值呢"）：复用上一轮标的重新取真实数据
        _ls = ((_chat_manager.context or {}).get('last_stock') or {}).get('code')
        if _ls:
            _fin_live = _finance_live_context('', code=_ls)
    if _fin_live:
        _fin_depth = None
        if not fast:
            try:
                from intent_router import determine_depth as _dd  # noqa_fin
                _fin_depth = _dd(message)
            except Exception:
                _fin_depth = None
        if fast or _fin_depth != 'deep':
            if fast:
                return {
                    'reply': _fin_live['context'],
                    'source': 'finance_live', 'llm_synthesized': False,
                    'intent': {'action': 'chat', 'params': {}, 'confidence': 0.9, 'raw_text': message},
                    'finance': {'code': _fin_live['code']},
                }
            try:
                from llm_client import chat
                sys_prompt = (
                    "你是用户身边靠谱的数据分析与金融顾问。用户询问股票估值/股价是否合理，"
                    "以下是系统自动拉取的实时数据（DCF 估值 + 财务比率），请基于这些真实数据简洁分析："
                    "先给结论（低估/合理/高估），再列 2-3 个关键依据，引用数据里的具体数字。"
                    "结尾必须注明：⚠️ 本结论为客观数据分析，仅供参考，不构成任何投资建议。"
                    + _depth_style('balanced')
                )
                _sk = _skills_context()
                if _sk:
                    sys_prompt = sys_prompt + "\n\n# 已加载技能（请遵循）\n" + _sk
                user_prompt = f"用户问题：{message}\n\n实时数据：\n{_fin_live['context']}"
                resp = chat([{'role': 'system', 'content': sys_prompt},
                             {'role': 'user', 'content': user_prompt}], llm_config, max_tokens=500)
                ans = (resp.get('content', '') if isinstance(resp, dict) else str(resp)).strip()
                if ans:
                    return {
                        'reply': ans, 'source': 'finance_live', 'llm_synthesized': True,
                        'intent': {'action': 'chat', 'params': {}, 'confidence': 0.9, 'raw_text': message},
                        'finance': {'code': _fin_live['code']},
                    }
            except Exception:
                pass
            return {
                'reply': _fin_live['context'],
                'source': 'finance_live', 'llm_synthesized': False,
                'intent': {'action': 'chat', 'params': {}, 'confidence': 0.9, 'raw_text': message},
                'finance': {'code': _fin_live['code']},
            }

    # 意图分流中间层（共享）：分类 + 同义词归一 + 角色/深度 + 金融角色增强 + 记忆
    _intent_router, _norm_msg, _role_prompt = _router_context(message, llm_config, current_file)
    _fin_meta = _intent_router.get('finance') or {}

    # 疑似指代追问但会话无任何历史标的（"那它现在贵不贵"无前文）→ 反问澄清，不联网瞎猜
    if _looks_like_dangling_reference(message):
        return {
            'ok': True,
            'reply': '🤔 你提到的「它」指哪只股票/基金？可以先说名称或代码，例如：\n'
                     '• "分析**兆易创新**的估值"（A股）\n'
                     '• "**腾讯**现在贵不贵"（港股）\n'
                     '或者先在顶部选择数据文件，我帮你分析当前文件。',
            'source': 'clarify',
            'intent': {'action': 'clarify', 'params': {}, 'confidence': 0.8, 'raw_text': message},
            'plan': {'steps': [], 'context': self_context_summary()},
        }

    # 智能运营分流（客户咨询应答/产品品类匹配/业务流引导）：确定性应答，不进 LLM 链路。
    # 让路规则内置在 try_ops_reply：挂文件或含分析动作词时一律交回主链路。
    if not force_web:
        _ops = try_ops_reply(message, current_file, fast=fast)
        if _ops:
            return _ops

    # 知识检索（知识点库 + 企业知识库）
    # P0-2 一致性：功能指引/排障类意图（怎么用/怎么导出/报错）不走知识库抢答，
    # 交给下方 help 分支返回本地功能清单，避免"怎么用这个网站"被知识条目误答。
    # P2 双轨收敛：路由明确判为「分析」时同样不抢答（见 _kb_should_yield）。
    _kb_blocked_by_intent = _kb_should_yield(message, _intent_router, current_file)
    if not force_web and not _kb_blocked_by_intent:
        try:
            # 检索 + 合并 + 金融子意图抑制 → 集中在共享决策函数（消除双路径漂移）
            from knowledge_rag import format_knowledge_answer
            merged, is_kb, ent_is_kb = _joined_kb_hits(message, _norm_msg, _fin_meta)
            if is_kb or ent_is_kb:
                if fast:
                    # 快速模式：直接返回知识点原文，不调 LLM（秒回）
                    answer = format_knowledge_answer(merged)
                    used_llm = False
                else:
                    # 思考模式：LLM 综合（有自己的思考），失败回退原文
                    _expert_ctx = _build_expert_context(
                        _norm_msg, depth=_depth_for_mode(_intent_router.get('depth') or 'balanced', fast))
                    _full_role = (_expert_ctx + "\n" + _role_prompt) if _expert_ctx else _role_prompt
                    answer = _rag_answer(_norm_msg, merged, llm_config,
                                         role_prompt=_full_role,
                                         depth=_depth_for_mode(_intent_router.get('depth') or 'balanced', fast))
                    used_llm = answer is not None
                    if not used_llm:
                        answer = format_knowledge_answer(merged)
                if answer:
                    # 统一增强：补「📚 依据」块 + 引用编号核验 + 分类时效免责
                    answer = kb_answer(answer, merged)
                    return {
                        'ok': True, 'reply': answer,
                        'source': 'knowledge', 'llm_synthesized': used_llm,
                        'knowledge': [e.get('name', '') for e in merged[:5]],
                        'mode': mode,
                        'intent': {'action': 'knowledge', 'params': {}, 'confidence': 0.95, 'raw_text': message},
                        'plan': {'steps': [], 'context': self_context_summary()},
                    }
        except Exception as e:
            logger.warning("知识点检索失败（不影响正常回答）: %s", e)

    # 普通聊天意图（旧正则仅作参数/计划来源；派发 action 见下方 P0-2 覆盖）
    result = _chat_manager.process_message(message, llm_config, current_file=current_file)
    # P0-2：最终派发 action 以 intent_router 判定为准，旧正则降为兜底，
    # 消除"两套意图系统判定漂移"（如 router 判 operation 却被按 chat 联网）。
    _regex_action = (result.get('intent') or {}).get('action')
    action = _effective_action(message, _intent_router, _regex_action)
    if isinstance(result.get('intent'), dict):
        result['intent']['action'] = action  # 下游分支与 result.intent 保持一致

    # 模糊/低置信度意图 → 引导式追问（路由中间层，先于执行计划；明确请求不受影响）
    _clarity = _maybe_clarify(message, _intent_router, current_file)
    if _clarity:
        return _clarity

    # 概念/方法型问题护栏（意图明确性）："什么是DID""夏普比率怎么算"会被
    # 正则识别器误判为 analyze_data（命中"分析/怎么"字样），进而在无文件时
    # 答非所问地要求用户上传数据文件。路由层已明确判定 knowledge 时，
    # 这类问题改走知识/联网回答，不需要任何数据文件。
    # P0-3 扩展：方法论问法（"如何做好数据分析"）同样不应落入"请上传文件"死胡同。
    _method_like = (_is_methodology_question(message)
                    and not _has_analyzable_target(message)
                    and not _wants_deep_report(message))
    if action == 'analyze_data' and not current_file and (
            _is_concept_question(message, _intent_router) or _method_like):
        _c_ctx = _build_expert_context(message, depth=_depth_for_mode(_intent_router.get('depth') or 'balanced', fast))
        _c_role = (_c_ctx + "\n" + _role_prompt) if _c_ctx else _role_prompt
        _c_ans = (_fast_chat_answer(message, llm_config, role_prompt=_c_role) if fast
                  else _web_search_answer(message, llm_config, role_prompt=_c_role,
                                          depth=_depth_for_mode(_intent_router.get('depth'), fast)))
        if _c_ans:
            return _c_ans

    if action in ('analyze_data', 'correlation', 'clean_data', 'train_model', 'create_chart'):
        # 功能询问前置拦截："网站能做什么/有什么功能/怎么用" 类问题（即使正则命中"分析"字样）先给本地帮助
        if any(w in message for w in ('怎么用', '怎么操作', '有哪些功能', '能做什么', '能干什么',
                                       '能帮我做什么', '能做什么分析', '网站能做什么', '怎么用这个',
                                       '有什么功能', '能分析什么', '可以做什么')):
            return {
                'ok': True,
                'reply': '我可以做这些事：\n\n'
                         '📊 **数据分析**：顶部选文件后说 **"帮我分析当前文件"**\n'
                         '🏦 **金融分析**：直接问股票/基金，如 **"分析兆易创新股价合理吗"**（自动联网取真实数据）\n'
                         '🧹 数据清洗 · 📈 画图 · 🔗 相关性 · 🔀 分组 · 🤖 机器学习 · 🎯 DID 因果\n'
                         '📚 **知识问答**：金融、税务、统计概念，如 **"什么是夏普比率"**\n'
                         '⚡ 快速模式秒回 · 🧠 思考模式联网深挖（输入框上方切换）',
                'source': 'fast',
                'intent': {'action': 'help', 'params': {}, 'confidence': 0.9, 'raw_text': message},
                'plan': {'steps': [], 'context': self_context_summary()},
            }

        # 金融类意图（行情/估值/行业板块/基金业绩/风险评估）不依赖上传数据文件：即使无文件也可用真实金融接口分析
        _fin_sub = _fin_meta.get('subintent')
        _is_fin_route = bool(_fin_sub and _fin_sub in _FIN_ROUTE_SUBINTENTS)
        if _is_fin_route:
            agent_ans = _run_agent_analysis(message, llm_config, current_file,
                                            depth=_depth_for_mode(_intent_router.get('depth'), fast), fast=fast)
            if agent_ans:
                return agent_ans
            # Agent 不可用（额度/超时）→ 无文件也继续分析：联网研究兜底，绝不退回"要文件"
            if not current_file:
                fb = _no_file_analysis_fallback(message, llm_config,
                                                depth=_intent_router.get('depth') or 'deep',
                                                role_prompt=_role_prompt)
                if fb:
                    return fb
        file_mentioned = (result.get('intent', {}).get('params', {}) or {}).get('file') or current_file
        plan_steps = (result.get('plan') or {}).get('steps') or []
        has_fetch_stock = any(
            (s.get('params') or {}).get('url') == '/api/finance/fetch-stock'
            for s in plan_steps)
        if not file_mentioned and not has_fetch_stock:
            # 无文件但消息带明确分析目标（行业/板块/股票/基金）、以指代延续上一轮、
            # 或显式要求"完整研究报告/深度分析"（P0-3 放行，避免落入"请上传文件"死胡同）→ 直接分析
            if (_has_analyzable_target(message) or _references_recent_target(message)
                    or _needs_structured_research(message) or _wants_deep_report(message)):
                agent_ans = _run_agent_analysis(message, llm_config, None,
                                                depth=_depth_for_mode(_intent_router.get('depth'), fast), fast=fast)
                if agent_ans:
                    return agent_ans
                fb = _no_file_analysis_fallback(message, llm_config,
                                                depth=_intent_router.get('depth') or 'deep',
                                                role_prompt=_role_prompt)
                if fb:
                    return fb
            return {
                'ok': True,
                'reply': '📂 我注意到你想做数据分析，但当前还没有选择数据文件，我也还不知道要分析什么。\n\n'
                         '两种方式任选：\n'
                         '1. **有数据文件**：在顶部选择文件（.csv / .xlsx），然后对我说 **"帮我分析当前文件"**；\n'
                         '2. **没有文件也可以**：直接告诉我要分析的行业、股票或基金，例如 **"全面分析电器行业"**、'
                         '**"分析腾讯值不值得买"**，我会联网获取最新资料来分析。\n'
                         '如果想了解数据分析的概念和方法，可以问我："什么是数据分析"。',
                'source': 'rules',
                'intent': {'action': 'show_message', 'params': {}, 'confidence': 0.9, 'raw_text': message},
                'plan': {'steps': [], 'context': self_context_summary()},
            }
        # 有文件 → Agent 分析（借助网站功能，输出结构化结论 + 建议操作）。
        # ⚡快速档也走 agent（关思考 + 收紧预算），仅跳过多 Agent 管线；
        # 此前 `and not fast` 会让快速模式带文件分析直接穿透到末尾 return result（无 reply → 空回复）。
        if file_mentioned:
            agent_ans = _run_agent_analysis(message, llm_config, file_mentioned,
                                            depth=_depth_for_mode(_intent_router.get('depth'), fast), fast=fast)
            if agent_ans:
                return agent_ans

    # 'knowledge' 纳入兜底：知识库未命中（如"帮我算一下夏普比率"实为计算类）时，
    # 仍需进入下方分支走真实计算/联网回答，而不是返回无 reply 的空壳 result。
    # 'nlq' 必须并入本兜底：此前它不在任何分派元组里 → 空回复（见 _chat_guard_reply 的 nlq 分支）
    chat_actions = ('chat', 'greeting', 'help', 'knowledge', 'nlq')
    if result.get('intent', {}).get('action') in chat_actions:
        _chat_action = result.get('intent', {}).get('action')
        # 本地兜底（问候/帮助/域外/计算/乱码）：逻辑集中在单一共享函数，消除双路径漂移
        _guard = _chat_guard_reply(message, _chat_action, _intent_router, result, current_file)
        if _guard:
            return _guard

        _depth_now = _intent_router.get('depth') or 'balanced'
        # 金融专家/约束/护栏上下文（D+A+B+E）合并到角色提示；按深度按需注入思维链（P1）
        _expert_ctx = _build_expert_context(message, depth=_depth_now)
        _full_role = (_expert_ctx + "\n" + _role_prompt) if _expert_ctx else _role_prompt
        # 真实计算注入（防幻觉 C）：显式计算请求（"帮我算一下夏普"）或带文件的数据问题
        # 时，把后端真实计算结果/护栏注入聊天上下文——LLM 只解读真值，不自行推算。
        _calc_extra = ""
        if current_file and _intent_router.get('intent') in ('nlq', 'analysis'):
            _calc_block = _compute_note(message, current_file)
            if _calc_block:
                _calc_extra = _calc_block
            elif action == 'nlq':
                # nlq 无专用计算器时，至少注入「真实数据摘要」（行数/列名/数值列统计），
                # 让 LLM 基于真实结构回答，而不是凭记忆编造数字。
                _brief = _nlq_data_brief(current_file)
                if _brief:
                    _calc_extra = _brief
        if fast:
            # 快速模式：不联网搜索，LLM 简短回答（更快）；失败给轻量兜底
            web_ans = _fast_chat_answer(message, llm_config, role_prompt=_full_role,
                                        extra_context=_calc_extra)
        elif _needs_web_search(message, _intent_router):
            # 思考模式：仅当问题需要最新/外部信息时联网搜索 + LLM 回答
            web_ans = _web_search_answer(message, llm_config, role_prompt=_full_role,
                                         depth=_depth_now, extra_context=_calc_extra)
        else:
            # 无需联网（闲聊/算术/概念直觉题）→ 直接 LLM 回答，避免无谓搜索与"思考过度"
            web_ans = _direct_answer(message, llm_config, role_prompt=_full_role,
                                     depth=_depth_now, extra_context=_calc_extra)
        if web_ans:
            return web_ans
    return result


def detect_special_intent_safe(message: str) -> str:
    """detect_special_intent 的安全包装（路由模块不可用时返回 generic）"""
    try:
        from intent_router import detect_special_intent
        return detect_special_intent(message)
    except Exception:
        return 'generic'


def _looks_like_question(message: str) -> bool:
    """宽松判断是否疑问句（用于企业知识库命中时的触发）"""
    from knowledge_rag import QUESTION_WORDS
    q = message.lower().strip()
    return any(w in q for w in QUESTION_WORDS) or '?' in q or '？' in q or len(q) <= 12


# ==================== 意图分流中间层（intent_router 接入）====================

_router_cache: Dict[str, tuple] = {}   # message -> (ts, result)
_ROUTER_CACHE_TTL = 10.0                # 同消息 10s 内复用路由结果（多路径重复调用时节省）


def _route_cache_key(message: str) -> str:
    """路由缓存键 = 会话维度 + 历史签名 + 文本。

    P1-3：`route()` 的结果依赖会话历史（指代消解 + DST 续接），旧键仅为 message 明文，
    导致 10s 内跨会话复用结论（实测：会话 B 本应澄清，却返回会话 A 的 analysis）。
    历史签名取（短期记忆轮数 + 末轮意图），足以区分「有无上下文」与「续接对象」。
    """
    try:
        cid = getattr(_chat_manager, 'active_conversation_id', 'legacy') or 'legacy'
    except Exception:
        cid = 'legacy'
    n, last = 0, ''
    try:
        from conversation_memory import get_memory
        _st = get_memory().short_term
        n = len(_st)
        last = _st[-1].get('intent', '') if _st else ''
    except Exception:
        pass
    return f"{cid}\x1f{n}\x1f{last}\x1f{message}"


def _route_intent(message: str, llm_config: dict = None, conversation_id: str = None):
    """轻量意图路由器（L1+L2④改写，Layer3 LLM 仅歧义时精排）。失败时返回空兜底，绝不阻断主流程。

    带进程级 TTL 缓存：同一消息在多条处理路径（知识/Agent/流式）被重复路由时复用结果，
    避免重复的规则匹配与（歧义时的）LLM 精排开销。
    conversation_id 显式给出时以之为缓存/历史维度，否则用当前激活会话。
    """
    _now = time.time()
    if conversation_id is not None:
        try:
            from conversation_memory import get_memory
            get_memory().set_active_conversation(
                _normalize_conversation_id(conversation_id))
        except Exception:
            pass
    _ckey = _route_cache_key(message)
    _cached = _router_cache.get(_ckey)
    if _cached and (_now - _cached[0]) < _ROUTER_CACHE_TTL:
        return _cached[1]
    try:
        # 用会话内短程记忆的历史做指代消解（④）与 DST 续接（携带上一轮意图），并做口语规范化
        _hist = []
        try:
            from conversation_memory import get_memory
            for t in get_memory().short_term[-3:]:
                _hist.append({"entities": t.get("entities", {}), "intent": t.get("intent")})
        except Exception:
            _hist = []
        from intent_router import rewrite_query, route as _r
        _rewritten = rewrite_query(message, _hist)
        _result = _r(_rewritten, history=_hist, llm_config=llm_config)
    except Exception as e:
        logger.debug("意图路由不可用（忽略）: %s", e)
        _result = {
            'intent': 'chat', 'action': 'chat', 'confidence': 0.5, 'entities': {},
            'needs_clarification': False, 'missing_info': [],
            'depth': 'balanced', 'role_prompt': '', 'normalized': message,
            'clarification': '',
        }
    # 清理过期缓存，避免无限增长
    if len(_router_cache) > 256:
        try:
            for _k in [k for k, (ts, _) in _router_cache.items() if _now - ts > _ROUTER_CACHE_TTL]:
                del _router_cache[_k]
        except Exception:
            pass
    _router_cache[_ckey] = (_now, _result)
    return _result


def _is_concept_question(message: str, router: dict) -> bool:
    """概念/方法型问题识别：路由判定为知识问答 + 概念/公式类措辞。

    用于拦截"什么是X""X怎么算"被正则识别器误判为 analyze_data 的情况，
    避免无文件时答非所问地引导用户上传数据。"""
    if not router or router.get('intent') != 'knowledge':
        return False
    markers = ('什么是', '是什么', '什么意思', '含义', '概念', '定义',
               '怎么算', '如何计算', '计算公式', '怎么理解', '如何理解', '区别')
    return any(w in message for w in markers)


def _has_analyzable_target(message: str) -> bool:
    """消息是否含可不依赖上传文件的分析目标：行业/板块/产业、股票标的、基金等"""
    if any(w in message for w in ('行业', '板块', '赛道', '产业', '基金', '股票', '大盘', '指数', '市场')):
        return True
    if _extract_stock_code(message):
        return True
    # 财务报表类请求：出现公司主体（股份/公司/集团/科技/银行等）+ 财务分析词 → 视为分析目标。
    # 覆盖"分析 XX 公司财务报表"这类未硬编码代码的合法问法，避免落入"请上传文件"死胡同。
    if any(k in message for k in _FIN_STATEMENT_KW) and any(
            t in message for t in ('股份', '公司', '集团', '科技', '银行', '证券', '电器', '汽车')):
        return True
    return False


def _needs_structured_research(message: str) -> bool:
    """无文件但需要联网「完整有条理分析」的问题：时事/市场/因果原因类。

    命中即应走 Agent 深度链路（web_search 取数 + 分维度结构化报告 + 思考可见），
    而不是退化成精简检索回答，更不应落入「请上传文件」提示。"""
    kw = ('为什么', '原因', '归因', '最新', '上涨', '下跌', '走高', '反弹', '回落',
          '走势', '趋势', '影响', '展望', '景气', '行情', '涨价', '降价', '供需', '新闻')
    return any(w in message for w in kw)


def _no_file_analysis_fallback(message: str, llm_config: dict, depth: str = 'deep',
                               role_prompt: str = '') -> Optional[Dict[str, Any]]:
    """无文件但有明确分析目标时的兜底：联网研究 + 专家角色生成分析。

    用于 Agent 不可用（额度/超时）时的降级——保证"无文件也能分析"，
    而不是退回要求上传文件的提示。"""
    _ctx = _build_expert_context(message, depth=depth or 'deep')
    _full_role = (_ctx + "\n" + role_prompt) if _ctx else role_prompt
    try:
        return _web_search_answer(message, llm_config, role_prompt=_full_role,
                                  depth=depth or 'deep')
    except Exception as e:
        logger.warning("无文件分析兜底失败: %s", e)
        return None


# 需要主动澄清的意图（operation/troubleshoot/agent/nlq/analysis 无文件时）
_CLARIFYABLE_INTENTS = {'operation', 'troubleshoot', 'agent', 'nlq', 'visualization', 'analysis',
                        # P0-3b：无信号输入（chat 低分档）与计算类含糊请求也应澄清，
                        # 而不是硬猜成闲聊 / 直接执行。明确应答语（"嗯""好的"）分档为 0.9，
                        # 不会走到澄清分支，因此把 chat 纳入不会误伤正常寒暄。
                        'chat', 'computation'}

# 可走 Agent/金融链路的金融子意图白名单（这些意图无文件也应直接分析，而不是要求文件或澄清）
_FIN_ROUTE_SUBINTENTS = (
    'market_quote', 'stock_valuation', 'fund_performance', 'risk_assessment',
    'stock_screening', 'quant_model', 'asset_allocation', 'sector_analysis')


def _maybe_clarify(message: str, router: dict, current_file: str) -> Optional[Dict[str, Any]]:
    """模糊意图 → 引导式追问（2.2）。仅在路由明确判定需要澄清且无更高优先级管线命中时触发。"""
    if not router or not router.get('needs_clarification'):
        return None
    intent = router.get('intent')
    if intent not in _CLARIFYABLE_INTENTS:
        return None
    # 有文件且是分析/查数/画图 → 交给对应流程，不澄清
    if current_file and intent in ('analysis', 'visualization', 'nlq'):
        return None
    # 金融路由子意图（行业/估值/行情等）不依赖上传文件 → 直接进入分析，不澄清
    _fin = router.get('finance') or {}
    if _fin.get('is_finance') and _fin.get('subintent') in _FIN_ROUTE_SUBINTENTS:
        return None
    text = router.get('clarification') or (
        "我在分析时还缺少一些关键信息，请补充后我再继续。")
    return {
        'ok': True, 'reply': text,
        'source': 'clarify',
        'intent': {'action': 'clarify', 'params': {}, 'confidence': router.get('confidence', 0.5), 'raw_text': message},
        'plan': {'steps': [], 'context': self_context_summary()},
    }


def _normalize_for_search(message: str) -> str:
    """金融同义词归一（4.2）：提升知识库/NLQ 命中率。失败原样返回。"""
    try:
        from intent_router import normalize_query
        return normalize_query(message)
    except Exception:
        return message


# ==================== 金融专家上下文块（D+A+B+E：专家角色/强思维链/业务约束/预测护栏）====================

def _estimate_complexity(message: str) -> int:
    """轻量问题复杂度估计（0-4）：多步骤/多目标/金融深析/长问 → 更复杂。
    用于语义分层（①）：复杂请求在同一个免费模型下加大投入。"""
    c = 0
    low = message.lower()
    complex_kw = ("为什么", "深入", "对比", "评估", "探讨", "分析一下", "全面", "研究报告",
                  "归因", "因果", "相关性", "多个", "维度", "建议",
                  "夏普", "回撤", "久期", "DID", "评分卡", "收益归因", "净值分解")
    for kw in complex_kw:
        if kw in low:
            c += 1
    if len(message) > 40:
        c += 1
    return min(4, c)


def _reasoning_note(message: str) -> str:
    """⑤ 思考摘要（替代 reasoning_content，零额外 LLM）：从问题本身提炼"分析要点"，
    供前端做折叠"思考过程"展示，提升可信度。"""
    try:
        from intent_router import detect_special_intent
        tag = detect_special_intent(message)
        tags = {
            "prediction": "先按不可预测原则，再做情景推演",
            "diagnosis": "按归因框架：数据→指标→业务原因",
            "computation": "基于后端真实计算，禁止自行推算",
        }
        return tags.get(tag, "按 问题拆解→数据→框架→陷阱 四步后作答")
    except Exception:
        return "按 问题拆解→数据→框架→陷阱 四步后作答"


def _message_depth(message: str) -> str:
    """消息深度判定（concise/balanced/deep）的安全包装"""
    try:
        from intent_router import determine_depth
        return determine_depth(message)
    except Exception:
        return 'balanced'


def _depth_style(depth: str) -> str:
    """按意图深度返回长度与结构要求：同一套风格随深度伸缩，避免死板的固定字数。

    deep   → 允许充分展开，但要求有反面视角与适用边界（有理有据、不一面之词）；
    concise→ 3 句话内直击要点；
    balanced→ 先结论后关键依据，自然表达。
    所有档位都禁模板套话（human_style 已覆盖语气，这里只管结构与长度）。
    """
    if depth == 'deep':
        return ("回答可充分展开（500-800字），建议结构：① 先给核心结论（关键数字**加粗**）；"
                "② 分点展开依据，引用具体数字或来源；"
                "③ 给出一个反方视角或主要风险点（避免一面之词）；"
                "④ 说明结论的适用边界（什么情况下不成立/证据还缺什么）。"
                "用自然段落或简短要点，禁用『综上所述』『首先/其次/最后』等模板衔接。")
    if depth == 'concise':
        return "回答控制在 3 句话以内，直接给结论，最多带一个关键数字。"
    return "回答简洁有力（200字左右）：先结论，后 1-2 条最关键依据；自然表达，不堆模板句。"


_FIN_SUBINTENT_ROLES = {
    "stock_valuation": "你是估值分析师：给出 PE/PB/DCF 等估值口径对比，先结论（高估/低估/合理）再列依据，明确区分事实估值与主观判断，不预测股价。",
    "risk_assessment": "你是风险分析师：量化回撤/波动/夏普、VaR 等风险收益指标，口径统一（复权/年化），明确指出回撤成因，不保证未来不会继续下行。",
    "fund_performance": "你是基金业绩分析师：只基于真实净值/收益率数据评价业绩，区分年化口径与区间收益，注明基金过往业绩不预示未来表现。",
    "market_quote": "你是行情播报助手：只引用真实/最新市场数据报价，明确数据时点与来源，不预测点位涨跌。",
    "sector_analysis": "你是行业研究员：基于公开资料做行业概况、市场规模、竞争格局、产业链、政策与风险分析；引用数据注明来源与时点，不编造市场份额与增速；结论区分事实与判断。",
    "macro_policy": "你是宏观策略分析师：基于政策/指标变化做影响推演（情景分析），区分事实与观点，不承诺利率/汇率走势。",
    "financial_statement": "你是财务分析师：基于三大报表真实科目分析盈利能力/偿债/营运，口径一致（同比/环比），不虚构财务数字。",
    "quant_model": "你是量化研究员：强调用真实回测/因子数据说话，明确过拟合/幸存者偏差风险，不承诺未来收益。",
    "stock_screening": "你是选股/选基顾问：基于可回测的筛选条件给出候选清单与排序逻辑，注明筛选依据与局限，不构成荐股。",
    "fixed_income": "你是固收分析师：基于收益率曲线/久期/利差做利率敏感分析，口径一致，提示利率变动风险。",
    "derivatives": "你是衍生品策略分析师：说明期权/期货的收益结构与风险（保证金/到期），明确杠杆风险，不承诺收益。",
    "credit_scoring": "你是风控建模专家：强调用真实样本训练与验证评分卡、关注 WOE/IV/PSI 与区分度，不泄漏个体隐私。",
    "asset_allocation": "你是资产配置顾问：基于风险偏好与真实资产数据给配置建议，明确分散化与再平衡逻辑，不承诺收益。",
    "policy_effect": "你是因果推断分析师：政策效果评估必须基于 DID/平行趋势/稳健性检验等因果框架，区分相关与因果。",
    "tax_compliance": "你是税务顾问：引用最新政策与法规，明确地区/时间口径差异，提示具体以官方最新发布为准。",
}


def _finance_role_for_subintent(subintent):
    """金融子意图 → 追加的专家角色/护栏提示；未命中返回空"""
    if not subintent:
        return ""
    role = _FIN_SUBINTENT_ROLES.get(subintent)
    if not role:
        return ""
    return role + "\n（涉及具体的数字、涨跌、收益均以真实数据或情景推演为准，附合规免责；不构成投资建议。）"


def _build_expert_context(message: str, depth: str = 'balanced') -> str:
    """
    组装金融专家与约束上下文块（作为 System/上下文前置，不可违反）：
      - 金融意图时：专家角色（D）
      - depth=='deep' 时：才追加「强制思维链 + 输出格式约束」，按需注入（P1）
      - 命中术语：公式硬约束 + 交易日历 + 合规表述（A+B）
      - 预测请求：风险护栏（E）
    返回拼接后的只读约束文本；失败静默返回空，不阻断。

    P1 改动：思维链（四步拆解）仅在 deep 深度下注入。此前对所有口径恒定注入，
    与"3 句话/200 字"约束自相矛盾，是无谓"思考过度/篇幅膨胀"的主因之一。
    """
    parts = []
    # 纯寒暄不进专家/约束/思维链（否则模型会推演免责并把思考写进回复）
    if (message or "").strip().lower() in ("你好", "您好", "hi", "hello", "嗨", "哈喽", "你好呀", "你好！"):
        return ""
    try:
        from financial_knowledge import (
            build_constraint_block, has_financial_msg,
            EXPERT_ROLE, THINKING_INSTRUCTIONS,
        )
        # 专家角色：金融问题才套用（避免"今天天气"也被塞金融人设）
        if has_financial_msg(message):
            parts.append(EXPERT_ROLE)
        # 思维链与格式约束：仅深度请求注入
        if depth == 'deep':
            parts.append(THINKING_INSTRUCTIONS)

        cons = build_constraint_block(message)
        if cons:
            parts.append(cons)

        # G. 场景化整体召回（金融场景知识块）
        try:
            from financial_knowledge import recall_scenario_blocks
            _scen = recall_scenario_blocks(message)
            if _scen:
                parts.append(_scen)
        except Exception:
            pass
    except Exception as e:
        logger.debug("专家上下文块构造失败（忽略）: %s", e)

    try:
        from intent_router import detect_special_intent, PREDICTION_GUARD
        if detect_special_intent(message) == "prediction":
            parts.append(PREDICTION_GUARD)
    except Exception:
        pass

    return "\n\n".join(p for p in parts if p)


def _compute_note(message: str, current_file: str) -> str:
    """
    真实计算结果注入（C）：识别计算类请求 → 调用后端真实计算 → 渲染为上下文。
    返回注入文本（可能为空）。LLM 只解读真实数字，禁止自行推算。
    """
    try:
        from real_compute import compute_and_render
        r = compute_and_render(message, current_file)
        return r.get("block", "")
    except Exception as e:
        logger.debug("真实计算注入失败（忽略）: %s", e)
        return ""


def _nlq_data_brief(current_file: str, max_cols: int = 15) -> str:
    """nlq 数据摘要：返回当前文件的**真实结构**（行数 / 列名 / 数值列统计 / 分类列取值）。

    用途：nlq（自然语言查数）在 real_compute 无专用计算器时，把真实数据摘要注入 LLM
    上下文，避免"用户要数字、模型凭记忆编造数字"。与 `_compute_note` 互补：
    前者给"算好的真值"，后者给"数据长什么样"。失败返回空串，绝不阻断主流程。
    """
    if not current_file:
        return ""
    try:
        from core_config import load_dataframe
        df = load_dataframe(current_file)
        if df is None or getattr(df, 'empty', True):
            return ""
        cols = [str(c) for c in df.columns]
        lines = [
            "# 当前数据（真实结构，回答须以此为准，禁止编造未给出的数字）",
            f"- 数据源：{current_file}",
            f"- 规模：{len(df)} 行 × {len(cols)} 列",
            f"- 列名：{', '.join(cols[:max_cols])}" + (" ..." if len(cols) > max_cols else ""),
        ]
        num = df.select_dtypes(include='number')
        if not num.empty:
            desc = num.describe().loc[['mean', 'min', 'max']]
            lines.append("- 数值列统计（均值 / 最小 / 最大）：")
            for c in list(desc.columns)[:max_cols]:
                lines.append(
                    f"    {c}: {desc.loc['mean', c]:.4g} / {desc.loc['min', c]:.4g} / {desc.loc['max', c]:.4g}")
        for c in list(df.select_dtypes(include='object').columns)[:5]:
            vals = df[c].dropna().astype(str).unique()[:5]
            if len(vals):
                lines.append(f"- 分类列「{c}」取值示例：{', '.join(vals)}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("数据摘要生成失败（忽略）: %s", e)
        return ""


# ==================== F. 双 Pass 轻量自校验（初版→攻击→终版）====================

_SELFCHECK_INTENTS = {'analysis', 'agent', 'computation', 'diagnosis'}


def _self_correct_answer(message: str, llm_config: dict, context: str, answer: str,
                         depth: str = 'balanced') -> Optional[str]:
    """
    轻量双 Pass 自校验：让 LLM 攻击初版答案（逻辑漏洞/数据口径/遗漏变量），再生成终版。
    仅在"深度分析"请求且初版入参充足时启用；失败回退初版，绝不阻断。
    """
    if depth != 'deep':
        # 只对深度/重要分析做自校验，避免普通问答翻倍消耗免费额度
        return None
    try:
        from llm_client import chat
    except Exception:
        return None
    if not message or not answer:
        return None
    user_prompt = (
        f"用户问题：{message}\n\n"
        f"（分析上下文/约束）\n{context[:2000]}\n\n"
        f"初版回答：\n{answer[:3000]}\n\n"
        f"请以严格审查者身份：\n"
        f"1. 指出初版回答中的逻辑漏洞、数据口径错误、遗漏的控制变量或假设；\n"
        f"2. 若涉及金融计算，核对其是否与【真实计算结果/公式约束】一致，指出任何编造或口径错误；\n"
        f"3. 基于审查给出综合校正后的最终回答（结论前置、可加表格、关键数字加粗、禁模板衔接词）。"
    )
    try:
        resp = chat([{"role": "system", "content": "你是一位严谨的金融风控审查官，专门负责挑错与校正。只输出校正后的最终回答，不要复述审查过程。"},
                     {"role": "user", "content": user_prompt}],
                    llm_config, max_tokens=1600)
        content = (resp.get("content") or "").strip() if isinstance(resp, dict) else str(resp).strip()
        return content if content else None
    except Exception as e:
        logger.debug("自校验失败（回退初版）: %s", e)
        return None


# ==================== 结构化建议（分析闭环）====================

# 建议操作 → 网站功能 API 映射（用户确认后前端 call_api 执行，覆盖全部分析模块）
# 含大量别名：LLM 输出的动作名多变（ts_decompose/group_data/train_model/create_chart/
# clean_data/analyze_data…），必须归一化到真实端点，否则采纳后不执行、只打勾。
_ACTION_API_MAP = {
    # ---- 描述与探索 ----
    'describe':      {'url': '/api/describe', 'method': 'POST', 'body': lambda p: {'file': p.get('file')}},
    'overview':      {'url': '/api/describe', 'method': 'POST', 'body': lambda p: {'file': p.get('file')}},
    'analyze_data':  {'url': '/api/describe', 'method': 'POST', 'body': lambda p: {'file': p.get('file')}},
    'correlation':   {'url': '/api/correlation', 'method': 'POST', 'body': lambda p: {'file': p.get('file')}},
    'group':         {'url': '/api/group', 'method': 'POST', 'body': lambda p: {'file': p.get('file'), 'group_col': p.get('group_col', '')}},
    'group_data':    {'url': '/api/group', 'method': 'POST', 'body': lambda p: {'file': p.get('file'), 'group_col': p.get('group_col', '')}},
    # ---- 清洗与表格 ----
    'clean':         {'url': '/api/clean', 'method': 'POST', 'body': lambda p: {'file': p.get('file')}},
    'clean_data':    {'url': '/api/clean', 'method': 'POST', 'body': lambda p: {'file': p.get('file')}},
    'pivot':         {'url': '/api/table/pivot', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'index_cols': [p['index']] if p.get('index') else [],
        'columns_col': p.get('column', ''),
        'values_col': p.get('values', ''),
        'agg_func': p.get('agg_func', 'mean'),
    }},
    'table':         {'url': '/api/table/pivot', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'index_cols': [p['index']] if p.get('index') else [],
        'columns_col': p.get('column', ''),
        'values_col': p.get('values', ''),
        'agg_func': p.get('agg_func', 'mean'),
    }},
    # ---- 图表 ----
    'chart':         {'url': '/api/chart/custom', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'x': [p['col1']] if p.get('col1') else [],
        'y': [p['col2']] if p.get('col2') else [],
        'type': p.get('chart_type', 'scatter'),
    }},
    'create_chart':  {'url': '/api/chart/custom', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'x': [p['col1']] if p.get('col1') else [],
        'y': [p['col2']] if p.get('col2') else [],
        'type': p.get('chart_type', 'scatter'),
    }},
    'plot':          {'url': '/api/chart/custom', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'x': [p['col1']] if p.get('col1') else [],
        'y': [p['col2']] if p.get('col2') else [],
        'type': p.get('chart_type', 'scatter'),
    }},
    # ---- 因果推断 ----
    'did':           {'url': '/api/did/full-analysis', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'dep_var': p.get('dep_var', ''),
        'treat_col': p.get('treat_col', 'treat'),
        'time_col': p.get('time_col', 'year'),
        'unit_col': p.get('unit_col', 'province'),
        'consume_cols': p.get('consume_cols', None),
    }},
    # ---- 统计检验 ----
    'ht':            {'url': '/api/ht/run', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'test_type': p.get('test_type', 'ttest_ind'),
        'value_col': p.get('value_col', ''),
        'group_col': p.get('group_col', ''),
        'group_a': p.get('group_a', ''),
        'group_b': p.get('group_b', ''),
    }},
    'hypothesis_test': {'url': '/api/ht/run', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'test_type': p.get('test_type', 'ttest_ind'),
        'value_col': p.get('value_col', ''),
        'group_col': p.get('group_col', ''),
    }},
    # ---- 时间序列 ----
    'ts':            {'url': '/api/ts/arima', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'date_col': p.get('date_col', ''),
        'value_col': p.get('value_col', 'value'),
        'forecast_periods': int(p.get('periods') or p.get('forecast_periods') or 12),
    }},
    'timeseries':    {'url': '/api/ts/arima', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'date_col': p.get('date_col', ''),
        'value_col': p.get('value_col', 'value'),
        'forecast_periods': int(p.get('periods') or p.get('forecast_periods') or 12),
    }},
    'ts_arima':      {'url': '/api/ts/arima', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'date_col': p.get('date_col', ''),
        'value_col': p.get('value_col', 'value'),
        'forecast_periods': int(p.get('periods') or p.get('forecast_periods') or 12),
    }},
    'ts_decompose':  {'url': '/api/ts/decompose', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'), 'value_col': p.get('value_col', 'value'),
    }},
    'ts_adf':        {'url': '/api/ts/adf', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'), 'value_col': p.get('value_col', 'value'),
    }},
    'ts_rolling':    {'url': '/api/ts/rolling', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'), 'value_col': p.get('value_col', 'value'),
        'window': int(p.get('window', 5)),
    }},
    # ---- 开放基金业绩指标（夏普/回撤/卡玛等）----
    'fund':          {'url': '/api/finance/fund-metrics', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'),
        'nav_col': p.get('nav_col', ''),
        'rf': float(p.get('rf', 0.02)) if p.get('rf') else 0.02,
    }},
    # ---- 机器学习 ----
    'ml':            {'url': '/api/ml/train', 'method': 'POST', 'body': lambda p: {
        'file_name': p.get('file') or p.get('file_name'),
        'task_type': p.get('task_type', 'regression'),
        'model_name': p.get('model_name', 'linear_regression'),
        'feature_cols': _parse_list_param(p.get('feature_cols')),
        'target_col': p.get('target_col', ''),
    }},
    'train_model':   {'url': '/api/ml/train', 'method': 'POST', 'body': lambda p: {
        'file_name': p.get('file') or p.get('file_name'),
        'task_type': p.get('task_type', '' if p.get('task_type') else 'regression'),
        'model_name': p.get('model_name', 'auto'),
        'feature_cols': _parse_list_param(p.get('feature_cols')),
        'target_col': p.get('target_col', ''),
    }},
    'regression':    {'url': '/api/ml/train', 'method': 'POST', 'body': lambda p: {
        'file_name': p.get('file') or p.get('file_name'),
        'task_type': 'regression', 'model_name': p.get('model_name', 'auto'),
        'feature_cols': _parse_list_param(p.get('feature_cols')),
        'target_col': p.get('target_col', ''),
    }},
    'classification': {'url': '/api/ml/train', 'method': 'POST', 'body': lambda p: {
        'file_name': p.get('file') or p.get('file_name'),
        'task_type': 'classification', 'model_name': p.get('model_name', 'auto'),
        'feature_cols': _parse_list_param(p.get('feature_cols')),
        'target_col': p.get('target_col', ''),
    }},
    'feature_eng':   {'url': '/api/feature/auto', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'), 'target': p.get('target_col', ''),
    }},
    # ---- 评分卡 ----
    'scorecard':     {'url': '/api/scorecard/train', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'), 'target': p.get('target_col', ''),
    }},
    # ---- 金融分析 ----
    'capm':          {'url': '/api/finance/capm', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'), 'return_col': p.get('return_col', ''), 'market_col': p.get('market_col', ''),
    }},
    'backtest':      {'url': '/api/finance/backtest', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'), 'strategy': p.get('strategy', ''), 'capital': p.get('capital', ''),
    }},
    'fama_french':   {'url': '/api/finance/fama-french', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'), 'return_col': p.get('return_col', ''),
    }},
    # ---- 自然语言查询 ----
    'nlq':           {'url': '/api/nlq/execute', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'), 'question': p.get('question', ''),
    }},
    # ---- 文本分析 ----
    'nlp':           {'url': '/api/nlp/analyze', 'method': 'POST', 'body': lambda p: {
        'file': p.get('file'), 'col': p.get('col', ''),
    }},
}

_ACTION_LABEL = {
    'describe': '📊 描述性统计', 'correlation': '🔗 相关性分析', 'group': '🔀 分组对比',
    'clean': '🧹 数据清洗', 'pivot': '📋 透视表', 'chart': '📈 生成图表',
    'did': '🎯 DID 因果分析', 'ht': '🔬 假设检验', 'ts': '📉 时间序列预测',
    'ml': '🤖 机器学习建模', 'nlp': '🔤 文本分析',
    'fund': '📊 基金业绩指标（夏普/回撤）',
}


def _parse_list_param(v):
    """把 'a,b,c' 或 ['a','b'] 统一转为 list"""
    if v is None:
        return []
    if isinstance(v, str):
        return [x.strip() for x in v.split(',') if x.strip()]
    if isinstance(v, list):
        return [str(x) for x in v]
    return []


def _parse_suggestions(output: str):
    """从 agent 输出解析【建议操作】：返回 (正文, 建议列表)"""
    suggestions = []
    for m in re.finditer(r'\[ACTION:([^\]]+)\]', output or ''):
        parts = m.group(1).strip().split()
        if not parts:
            continue
        name = parts[0].strip()
        params = {}
        for kv in parts[1:]:
            if '=' in kv:
                k, v = kv.split('=', 1)
                params[k.strip()] = v.strip()
        suggestions.append({'action': name, 'params': params, 'label': _ACTION_LABEL.get(name, name)})
    reply = re.sub(r'\[ACTION:[^\]]+\]', '', output or '')
    reply = re.sub(r'【建议操作】\s*', '', reply)
    reply = re.sub(r'\n{3,}', '\n\n', reply).strip()
    return reply, suggestions


def _suggestion_to_step(sug: dict, current_file: str):
    """建议 → 前端 plan 步骤（call_api，自动同步图表到报告）。

    - 动作名归一化（去空格/小写），命中别名表或主表即映射到真实端点；
    - 未知动作**兜底为 describe**（真实执行），绝不返回 None 静默丢弃，
      否则用户点击『采纳并执行』只会看到打勾而无实际执行。
    """
    raw = (sug.get('action') or '').strip().lower()
    api = _ACTION_API_MAP.get(raw)
    if not api:
        api = _ACTION_API_MAP.get('describe')
    params = dict(sug.get('params') or {})
    if current_file:
        params['file'] = current_file
    body = api['body'](params)
    return {
        'id': 'sug_' + raw,
        'action': 'call_api',
        'params': {'url': api['url'], 'method': api['method'], 'body': body},
        'description': sug.get('label', raw),
    }


def _references_recent_target(message: str) -> bool:
    """消息是否以指代/省略方式延续上一轮的分析标的。

    覆盖三类真实说法（依赖会话上下文 last_stock，由 _finance_live_context 取数时记录）：
    - 指代词："对当前数据深入分析"、"那它的估值呢"、"帮他看看"
    - 超短金融追问："股价"、"估值合理吗"、"多少钱"（≤6 字 + 金融关键词）
    - 省略主语的动作："走势怎么样"、"前景如何"
    """
    try:
        last = (_chat_manager.context or {}).get('last_stock') or {}
        if not last.get('code'):
            return False
        if any(w in message for w in ('当前', '上面', '刚才', '这个', '该', '继续', '此',
                                      '他', '她', '它', '那')):
            return True
        if len(message.strip()) <= 6 and any(k in message for k in _FIN_INTENT_KW):
            return True
        # 无主语的动作短语："走势怎么样"、"前景如何"、"业绩怎么样"
        if len(message.strip()) <= 12 and any(w in message for w in ('走势', '前景', '业绩', '表现')):
            return True
        return False
    except Exception:
        return False


def _looks_like_dangling_reference(message: str) -> bool:
    """疑似指代/省略式追问，但会话里没有任何历史标的（股票/文件）。

    场景："那它现在贵不贵" "它的估值呢" "这只股票怎么样"——无前文时不应联网瞎猜，
    而应澄清所指对象。仅在消息带指代词 + 金融追问词 + 无新标的、且无任何 last_stock 时命中。
    """
    try:
        last = (_chat_manager.context or {}).get('last_stock') or {}
        if last.get('code'):
            return False  # 有历史标的 → 真指代，交给 _references_recent_target 处理
        _ref_words = ('它', '那', '该', '此', '这', '这只', '这个', '刚才', '上面', '上面那', '之前那')
        _ask_words = ('贵', '估值', '股价', '走势', '行情', '前景', '表现', '业绩', '值不值',
                      '合理', '涨', '跌', '怎么样', '如何', '多少钱', '还能买', '值得')
        has_ref = any(w in message for w in _ref_words)
        has_ask = any(w in message for w in _ask_words)
        if not (has_ref and has_ask):
            return False
        # 若消息自带新标的（代码/名称）则不澄清
        if resolve_stock_symbol(message):
            return False
        return True
    except Exception:
        return False


# ⚡快速档（⚡快速模式下的分析请求）迭代预算：一轮取数 + 一轮收尾。
# 依据：快速模式此前与深度思考传入 agent 的参数逐项相同（file/depth/max_iter/agent_msg
# 全一致），导致"快速"没有任何提速。收紧到 2 轮 + 关闭全链路思考后，仍保留真实工具取数
# （数字不编造），但不再做多轮深挖与二次辩论。
_FAST_AGENT_MAX_ITER = 2


def _prepare_agent_run(message: str, llm_config: dict, current_file: str,
                       fast: bool = False) -> Dict[str, Any]:
    """组装 Agent 深度分析的输入（同步/流式两条路径共享，精简自原 _run_agent_analysis）。

    组成：复杂度分层(①) + 长期记忆偏好(⑥) + 专家/约束上下文(D+A+B+E) +
    真实计算注入(C) + 金融实时数据注入 + 无文件模式引导。

    fast=True（⚡快速档）：关闭整条 agent 链路的模型思考 + 收紧迭代预算。
    默认 False 时逐字节保持原行为（深度思考 / 同步路径零回归）。"""
    # ① 语义分层：按复杂度调节 agent 投入（temp/迭代）。复杂+深析 → 更收敛(低温)+更充分上下文
    _complex = _estimate_complexity(message)
    eff_cfg = llm_config
    if _complex >= 2 and llm_config:
        eff_cfg = dict(llm_config)
        eff_cfg["temperature"] = 0.3  # 深度复杂请求更严谨
    # 迭代预算统一收敛到 agent_loop 的意图/复杂度动态预算（'auto'），避免这里被硬编码覆盖。
    max_iter = 'auto'
    if fast:
        # ⚡快速档：规划/工具决策/收尾一律不思考。快速模式要的是"尽快给出基于真实工具结果的
        # 结论"，思考的边际收益低而耗时占比最高（实测 8 次决策调用各 3~6s）。
        # 仅在 provider 支持该字段时下发（与 _finalize_agent_result / agent_pipeline 同口径，
        # 避免对不支持 reasoning_effort 的 provider 造成 400）。
        eff_cfg = dict(eff_cfg or {})
        if str(eff_cfg.get('provider') or 'sensenova') == 'sensenova':
            eff_cfg['reasoning_effort'] = 'none'
        max_iter = _FAST_AGENT_MAX_ITER

    # 长期记忆偏好提示（用户常用数据集/指标），作为轻量上下文注入，不阻断
    pref_hint = ""
    try:
        from conversation_memory import get_memory
        pref_hint = get_memory().get_full_context()   # ⑥ 含会话摘要压缩 + 偏好
    except Exception:
        pref_hint = ""

    # 会话内最近分析结论（记忆真正接进主循环）："接着上次的分析"可直接续接，
    # Agent 不再只看当前 turn——上一轮的结论要点以轻量文本注入（零额外 LLM 调用）
    recent_concl = ""
    try:
        concl_msgs = [m for m in list(_chat_manager.history)  # P1-9：快照迭代
                      if getattr(m, 'role', '') == 'assistant' and (m.content or '').strip()][-3:]
        if concl_msgs:
            recent_concl = "【会话内最近结论（用户追问时可直接续接，数字以最新取数为准）】\n" + "\n".join(
                f"- {m.content.strip()[:180]}" for m in concl_msgs)
    except Exception:
        recent_concl = ""

    # 金融专家/约束上下文 + 真实计算结果注入（D+A+B+E + C）
    expert_ctx = _build_expert_context(message, depth='deep')
    calc_note = _compute_note(message, current_file)
    # 金融实时数据（股价/DCF/财务比率/技术面/相对估值）真实结果注入 Agent：
    # 避免深度分析时凭记忆编造价格与指标，并引导输出结构化全面报告（非仅财务）。
    # 多轮连续性：消息不含标的但以指代词延续上一轮时，复用 last_stock 重新取数。
    fin_live_note = ""
    try:
        _fin_live = _finance_live_context(message, use_cache=False)
        if not _fin_live and _references_recent_target(message):
            _last_code = ((_chat_manager.context or {}).get('last_stock') or {}).get('code')
            if _last_code:
                _fin_live = _finance_live_context('', code=_last_code, use_cache=False)
                if _fin_live:
                    fin_live_note = (
                        f"# 多轮连续性：以下是用户所指『当前数据』对应标的（{_last_code}）的最新真实取数\n\n")
        if _fin_live and _fin_live.get('context'):
            fin_live_note = fin_live_note + (
                f"# 已检索到的该标的最新金融数据（真实接口结果，务必以这些数据为准，勿编造）\n"
                f"{_fin_live['context']}\n\n"
                f"请基于以上真实数据输出一份结构化的全面投资分析报告，至少覆盖："
                f"①公司/行业概况 ②护城河与商业模式 ③财务质量（盈利/成长/偿债）④绝对估值(DCF) ⑤相对估值(可比PE/PB) "
                f"⑥技术面/价格位置 ⑦主要风险 ⑧综合结论与操作建议。"
                f"数据不足的方面明确标注，不得编造或臆断。"
            )
    except Exception as e:
        logger.debug("金融实时数据注入 Agent 失败（忽略）: %s", e)

    ctx_extra = "\n\n".join(p for p in (expert_ctx, fin_live_note, calc_note, pref_hint, recent_concl) if p)
    # 无文件分析模式：明确告知 Agent 走联网/技能/金融工具，不要空转在文件类工具上
    if not current_file:
        ctx_extra = (ctx_extra + "\n\n" if ctx_extra else "")
        ctx_extra += (
            "# 无文件分析模式（重要）\n"
            "当前未选择数据文件：不要调用 columns/describe/head/group/correlation 等文件类工具。\n"
            "- 行业/公司/宏观类分析：用 web_search 工具检索最新公开资料（行业规模、竞争格局、政策、新闻），"
            "并可先用 list_skills / run_skill 加载匹配的行业研究技能；\n"
            "- 具体股票/基金标的数据：优先用金融工具获取真实行情/估值/财务数据；\n"
            "- 结论注明信息来源与时点，资料不足的方面如实说明，不得编造数字。"
        )
    agent_msg = (f"{message}\n\n{ctx_extra}") if ctx_extra else message
    return {'eff_cfg': eff_cfg, 'max_iter': max_iter, 'agent_msg': agent_msg,
            'expert_ctx': expert_ctx, 'calc_note': calc_note}


def _finalize_agent_result(result: dict, message: str, llm_config: dict,
                           current_file: str, prep: dict, depth: str = None,
                           fast: bool = False):
    """agent_loop 结果 → 前端响应（建议解析/计划步骤/自校验/耗时透传）。

    fast=True（⚡快速档）跳过"双 Pass 自校验"（额外一次 LLM 调用，与"快"相悖）。
    无有效回复返回 None（由调用方决定重试或兜底）。"""
    if not result or not result.get('ok'):
        return None
    output = result.get('output', '')
    reply, suggestions = _parse_suggestions(output)
    if not reply:
        return None
    plan_steps = [_suggestion_to_step(s, current_file) for s in suggestions]
    plan_steps = [s for s in plan_steps if s]
    # 同动作可能重复出现，id 加序号保证唯一，避免前端 getElementById 冲突
    _seen = {}
    for _st in plan_steps:
        _base = _st['id']
        _seen[_base] = _seen.get(_base, -1) + 1
        if _seen[_base] > 0:
            _st['id'] = f"{_base}_{_seen[_base]}"

    # 双 Pass 自校验（F）：仅在本次 Agent 出现过工具调用失败时才二次校正（DA×F 中的"对分
    # 支纠偏"），一次成型、无工具失败的干净报告直接返回，避免每个深度分析都额外再耗 1 次
    # LLM 调用（免费额度下既慢又放大 429 窗口）。已由辩论引擎复核过的也不再重复自校验。
    try:
        _depth = depth
        if not _depth:
            from intent_router import determine_depth as _dd
            _depth = _dd(message)
        _trc = result.get('trace') or {}
        _had_tool_errors = int(_trc.get('errors') or 0) > 0
        _already_reviewed = bool(result.get('debugged_by_debate'))
        # P2-6：优先用结构化降级标记（agent 链路在 result['degraded'] 显式声明），
        # 字符串匹配仅作旧路径兼容，降低与兜底文案的魔法耦合
        _is_fallback = bool(result.get('degraded')) \
            or ('未生成文字报告' in reply) or ('已完成的资料查询' in reply)
        if (not fast and _depth == 'deep' and not _is_fallback and len(reply) > 80
                and _had_tool_errors and not _already_reviewed):
            _corrected = _self_correct_answer(message, llm_config,
                                               context=prep['expert_ctx'] + "\n" + prep['calc_note'],
                                               answer=reply, depth=_depth)
            if _corrected:
                reply = _corrected
    except Exception as e:
        logger.debug("自校验回退初版: %s", e)

    return {
        'ok': True,
        'reply': reply,
        'source': 'agent',
        'reasoning': _reasoning_note(message),   # ⑤ 思考摘要（前端可折叠展示）
        'suggestions': suggestions,
        'calls': result.get('calls', []),
        'usage': result.get('usage', {}),
        'elapsed_ms': result.get('elapsed_ms'),
        'trace': result.get('trace'),
        # ---- P1-5：透传降级/截断/轨迹标识（旧实现白名单重建 dict 全部丢弃，
        # 导致"被轮次上限截断的报告"按完整报告展示，且 340 份轨迹资产无前端入口）----
        # 语义拆分（2026-09-18）：truncated=终稿被 max_tokens 截断；force_final=达轮次上限收尾（正常）。
        'truncated': bool(result.get('truncated')),
        'force_final': bool(result.get('force_final')),
        'degraded': bool(result.get('degraded')),
        'end_status': result.get('end_status') or '',
        'resumable': bool(result.get('resumable')),
        'trace_id': result.get('trace_id') or '',
        'trace_file': result.get('trace_file') or '',
        'iterations': result.get('iterations'),
        # 事实核查（数据必须真实）：核验数据清单 + 有文件零工具提示
        # （旧白名单重建 dict 会丢掉这些字段，前端无法展示核验结果）
        'fact_audit': result.get('fact_audit'),
        'no_tool_with_file': bool(result.get('no_tool_with_file')),
        'fact_unverified_high': bool(result.get('fact_unverified_high')),
        'intent': {'action': 'analyze_data', 'params': {'file': current_file}, 'confidence': 0.9, 'raw_text': message},
        'plan': {'steps': plan_steps, 'context': self_context_summary()},
    }


def _maybe_debate(message: str, llm_config: dict, current_file: str):
    """显式要求"辩论/多角度"时走对抗式辩论引擎（3.2）；命中返回结果 dict，否则 None。"""
    try:
        from debate_engine import debate_analysis, wants_debate
        if wants_debate(message):
            _db = debate_analysis(message, llm_config, current_file)
            if _db and _db.get('reply'):
                return {
                    'ok': True,
                    'reply': _db['reply'],
                    'source': 'debate',
                    'calls': _db.get('rounds', []),
                    'usage': _db.get('usage', {}),
                    'intent': {'action': 'analyze_data', 'params': {'file': current_file}, 'confidence': 0.9, 'raw_text': message},
                    'plan': {'steps': [], 'context': self_context_summary()},
                }
    except Exception as e:
        logger.warning("辩论引擎不可用，回退标准 Agent: %s", e)
    return None


def _run_agent_analysis(message: str, llm_config: dict, current_file: str, depth: str = None,
                        fast: bool = False):
    """分析请求走 Agent（同步版）：解析结构化结论 + 建议操作。

    显式要求"辩论/多角度/不同观点"时先走辩论引擎；否则走标准 ReAct Agent。
    fast=True（⚡快速档）跳过辩论与评审式纠偏（两者都是多轮 LLM，与"快"相悖）。
    失败返回 None（由调用方决定兜底）。"""
    if not fast:
        debate = _maybe_debate(message, llm_config, current_file)
        if debate:
            return debate

    prep = _prepare_agent_run(message, llm_config, current_file, fast=fast)

    # 始终使用用户所选模型（如日日新 deepseek-v4-flash），不降级；
    # 输出残缺（云端限流 429 中断，间歇限流恢复后可能成功）时用同一配置重试一次
    def _run(cfg):
        from ai_agent import agent_loop
        return agent_loop(
            user_message=prep['agent_msg'], llm_config=cfg,
            file_name=current_file, max_iterations=prep['max_iter'],
            fast=fast,   # ⚡快速档：终稿收紧预算并要求核心结论
        )

    try:
        result = _run(prep['eff_cfg'])
        # 评审式多 Agent 纠偏：首次深度分析若发生过工具调用失败（结论可能站不稳，
        # 数据不足/取数失败），用对抗式辩论（skeptic 质询→expert 修正→synthesizer 收敛）
        # 对已有结论做二次复核，降低"单次失败工具导致的分析偏差"。
        # 仅在 trace.errors>0 且产出非空时触发，其余场景零额外成本，保护免费额度。
        _trc = (result.get('trace') or {}) if isinstance(result, dict) else {}
        _errs = int(_trc.get('errors') or 0)
        _out = ((result.get('output') or '') if isinstance(result, dict) else '').strip()
        if (not fast) and _errs > 0 and len(_out) > 60 and current_file:
            try:
                from debate_engine import debate_analysis
                _ld = debate_analysis(prep['agent_msg'], llm_config, current_file,
                                      initial_analysis=_out)
                if _ld and _ld.get('reply'):
                    result['output'] = _ld['reply']
                    result['debugged_by_debate'] = True
                    result['source'] = result.get('source') or 'agent+debate'
            except Exception as _e:
                logger.debug("评审式辩论纠偏跳过: %s", _e)
        ans = _finalize_agent_result(result, message, llm_config, current_file, prep, depth, fast=fast)
        if ans:
            return ans
        logger.warning("Agent 无有效回复，同一配置重试一次: output=%r", (result.get('output') or '')[:60])
        try:
            result = _run(prep['eff_cfg'])   # 沿用 eff_cfg（快速档的关思考/调温不能被重试丢掉）
            ans = _finalize_agent_result(result, message, llm_config, current_file, prep, depth, fast=fast)
            if ans:
                return ans
        except Exception as e2:
            logger.warning("Agent 重试失败: %s", e2)
        # 两轮 Agent 均失败：降级为确定性兜底报告（有文件走数据驱动、无文件走联网聚合），避免空结果
        return _deterministic_fallback_report(message, current_file)
    except Exception as e:
        logger.warning("Agent 分析失败（回退规则流程）: %s", e)
        return None


def _file_data_fallback_report(message: str, current_file: str) -> Optional[Dict[str, Any]]:
    """Agent 深度分析因 LLM 限流/超时彻底失败时的确定性兜底：不依赖 LLM，
    用平台真实计算引擎生成数据驱动报告，保证「深度分析不再产出空报告」。

    返回与 Agent 一致的 done 数据结构（reply 为渲染后的可读报告，source=agent_fallback）；
    当前文件无法读取或引擎不可用时返回 None（由调用方决定最终兜底文案）。"""
    if not current_file:
        return None
    try:
        from core_config import load_dataframe, OUT_DIR
        # 修复：真实类名是 EnhancedAnalysisEngine（ai_analysis_engine.py:25）。
        # 原写法 `AiAnalysisEngine` 恒 ImportError，被下方 except 吞掉 → 兜底报告恒返回 None，
        # 违反本函数 docstring「深度分析不再产出空报告」的承诺，故为 LLM 失败兜底的死代码。
        from ai_analysis_engine import EnhancedAnalysisEngine
        path = OUT_DIR / current_file
        df = load_dataframe(str(path))
        if df is None or df.empty:
            return None
        result = EnhancedAnalysisEngine(df, current_file).full_analysis(message)

        rows = result.get('overview', {}).get('rows', len(df))
        cols = result.get('overview', {}).get('cols', df.shape[1])
        missing = result.get('overview', {}).get('missing_rate', 0)
        lines = []
        lines.append(f"# 数据驱动分析报告 · {current_file}")
        lines.append("")
        lines.append(f"> ⚠️ 深度分析模型本轮未能生成完整报告（多为 LLM 限流/超时所致）。以下为**平台基于真实数据计算的确定性分析结果**，数字均来自身份真实计算，可直接引用。")
        lines.append("")
        lines.append(f"**数据概览**：{rows} 行 × {cols} 列，整体缺失率 {missing:.2f}%。")
        # 概览统计
        _stats = result.get('overview', {}).get('stats') or {}
        if _stats:
            lines.append("")
            lines.append("**数值列分布（均值 / 标准差 / 最小 / 最大）**：")
            _names = list(_stats.keys())[:8]
            for c in _names:
                s = _stats[c]
                lines.append(f"- `{c}`：mean={s.get('mean')}, sd={s.get('std')}, min={s.get('min')}, max={s.get('max')}")
        # 关键发现
        _findings = result.get('key_findings') or []
        if _findings:
            lines.append("")
            lines.append("**关键发现**：")
            for f in _findings[:6]:
                lines.append(f"- {f}")
        # 相关性与因果提示
        _corr = result.get('correlations') or {}
        _pairs = _corr.get('top_pairs') or []
        if _pairs:
            lines.append("")
            lines.append("**最强相关关系（|r| 降序）**：")
            for a, b, r in _pairs[:5]:
                lines.append(f"- `{a}` ↔ `{b}`：r={r}")
        _hints = _corr.get('causal_hints') or []
        if _hints:
            lines.append("")
            lines.append("**可能关联方向（注意：相关≠因果）**：")
            for h in _hints[:3]:
                lines.append(f"- {h}")
        # 风险点
        _risks = result.get('risk_points') or []
        if _risks:
            lines.append("")
            lines.append("**主要风险点**：")
            for r in _risks[:5]:
                lines.append(f"- {r}")
        # 建议
        _recs = result.get('recommendations') or []
        if _recs:
            lines.append("")
            lines.append("**下一步建议**：")
            for r in _recs[:5]:
                lines.append(f"- {r}")
        # 面板/DID 诊断
        _panel = result.get('panel_diagnostics') or {}
        if _panel.get('message') and '非面板' not in _panel['message']:
            lines.append("")
            lines.append(f"**面板诊断**：{_panel.get('message')}")
        _did = result.get('did_regression') or {}
        if _did and _did.get('has_treat') is True:
            lines.append("")
            lines.append("**DID 回归结论**：数据含处理组与时间维度，详见上方过程记录中的检验结果。")
        lines.append("")
        lines.append("---")
        lines.append("> 如需完整的多 Agent 深度分析（含经济机制解释与策略建议），可在模型限流恢复后重试，或切换付费模型。")

        reply = "\n".join(lines)
        return {
            'ok': True,
            'reply': reply,
            'source': 'agent_fallback',
            'reasoning': _reasoning_note(message),
            'intent': {'action': 'analyze_data', 'params': {'file': current_file}, 'confidence': 0.9, 'raw_text': message},
            'plan': {'steps': [], 'context': self_context_summary()},
        }
    except Exception as e:
        logger.warning("数据驱动兜底报告生成失败: %s", e)
        return None


def _web_search_fallback_report(message: str) -> Optional[Dict[str, Any]]:
    """无文件、且 LLM 限流/超时彻底失败时的确定性兜底：不依赖 LLM 综合，
    直接用联网搜索（web_search 多源爬取）聚合最新公开资料，拼成可读报告，
    满足「原因/趋势/最新动态类问题」即使模型不可用也能给出有据可查的结果。

    返回与 Agent 一致的 done 数据结构（reply 为拼接的搜索聚合报告，source=web_fallback）；
    搜索全部失败时返回 None（由调用方决定最终兜底文案）。"""
    try:
        from web_search import web_search
    except Exception as e:
        logger.warning("web_search 模块不可用: %s", e)
        return None
    try:
        res = web_search(message, top_k=6)
    except Exception as e:
        logger.warning("联网兜底搜索异常: %s", e)
        return None
    items = (res.get('results') or []) if isinstance(res, dict) else []
    if not res.get('ok') or not items:
        logger.warning("联网兜底搜索无结果: %s", res.get('error', '未知'))
        return None

    lines = []
    lines.append(f"# 联网资料聚合报告")
    lines.append("")
    lines.append(f"> ⚠️ 深度分析模型本轮未能生成完整报告（多为 LLM 限流/超时所致）。以下为**平台直接联网采集的最新公开资料汇总**，来源与摘要如实列示，可点击链接核实原文。")
    lines.append("")
    lines.append(f"**问题**：{message}")
    lines.append("")
    for i, it in enumerate(items, 1):
        title = (it.get('title') or '').strip()
        url = (it.get('url') or '').strip()
        snip = (it.get('snippet') or '').strip()
        lines.append(f"### {i}. {title}")
        if url:
            lines.append(f"来源：{url}")
        if snip:
            lines.append(f"摘要：{snip}")
        lines.append("")
    lines.append("---")
    lines.append("> 以上为联网搜索的原文聚合，未做模型归纳。如需结合多来源做对比、归因与结论，可在限流恢复后重试，或切换付费模型。")

    return {
        'ok': True,
        'reply': "\n".join(lines).strip(),
        'source': 'web_fallback',
        'reasoning': _reasoning_note(message),
        'web': {'sources': [{'title': it.get('title', '')[:60], 'url': it.get('url', '')} for it in items[:6]]},
        'intent': {'action': 'analyze_data', 'params': {'file': None}, 'confidence': 0.9, 'raw_text': message},
        'plan': {'steps': [], 'context': self_context_summary()},
    }


def _stalled_result(message: str, current_file: str) -> Dict[str, Any]:
    """P1-4③：流式分析卡死（长时间无进展事件）时的收尾结果。

    优先给出确定性兜底报告（有文件走真实计算 / 无文件走联网聚合），
    再退化为诚实提示；两种都带 degraded/end_status 标记，供前端提示"本次未完整生成"。
    """
    try:
        _fb = _deterministic_fallback_report(message, current_file)
        if _fb:
            _fb['degraded'] = True
            _fb['end_status'] = 'stalled'
            return _fb
    except Exception as e:
        logger.debug('卡死兜底报告生成失败: %s', e)
    return {
        'ok': True, 'degraded': True,
        'end_status': 'stalled', 'source': 'agent',
        'reply': (f'⚠️ 深度分析超过 {int(_AI_STALL_LIMIT_S)} 秒无进展（外部数据源或模型长时间无响应），'
                  '已主动中止以避免长时间占用线程。\n\n'
                  '已完成的过程记录见上方「思考过程」面板；请稍后重试，'
                  '或减少数据规模、切换模型后再试。'),
        'intent': {'action': 'analyze_data', 'params': {'file': current_file}, 'confidence': 0.9, 'raw_text': message},
        'plan': {'steps': [], 'context': self_context_summary()},
    }


def _deterministic_fallback_report(message: str, current_file: str) -> Optional[Dict[str, Any]]:
    """Agent 深度分析在 LLM 限流/超时彻底失败时的总兜底入口：
    - 选择了文件 → 用平台真实计算引擎生成数据驱动报告；
    - 未选文件（原因/趋势/最新类问题）→ 用联网搜索聚合最新公开资料；
    两者都不依赖 LLM 生成正文，保证「深度分析不再产出空报告」。
    均不可用时返回 None（由调用方给出诚实提示）。"""
    if current_file:
        return _file_data_fallback_report(message, current_file)
    return _web_search_fallback_report(message)


def _stream_agent_analysis(message: str, llm_config: dict, current_file: str, depth: str = None,
                           session_id: str = None, fast: bool = False):
    """流式分析生成器（Trae/Zcode 级体验的核心）：
    Agent 后台线程执行，计划/工具调用/耗时等进度事件经队列实时转发为 SSE。

    fast=True（⚡快速档）：跳过辩论、关闭全链路思考、迭代预算收紧到 2 轮，
    状态提示改为"⚡快速分析中"——此前快速模式沿用"🧠 深度分析启动"文案，既与用户
    所选模式不符，也掩盖了"两种模式走同一条链路、耗时几乎一致"的事实。

    事件流：status(文本进度) → progress(结构化: plan|tool_start|tool_end) → ping(心跳,保活)
    → done(最终结果)。done 数据结构与 _run_agent_analysis 完全一致（含 calls/usage/elapsed_ms/trace）。
    session_id 传入时支持前端"停止生成"联动后端取消（is_cancelled 检查点）。"""
    import queue as _queue
    import threading as _threading

    HEARTBEAT_S = _AI_HEARTBEAT_S  # 长任务期间定期发 ping，防止代理/网关空闲断连

    if not fast:
        debate = _maybe_debate(message, llm_config, current_file)
        if debate:
            yield {'type': 'done', 'data': debate}
            return

    prep = _prepare_agent_run(message, llm_config, current_file, fast=fast)
    yield {'type': 'status', 'message': (
        '⚡ 快速分析中：取数 → 研判...' if fast
        else '🧠 深度分析启动：制定计划 → 分步取数 → 综合研判...')}

    evq: _queue.Queue = _queue.Queue()

    def _cb(evt):
        try:
            evq.put(dict(evt))
        except Exception:
            pass

    def _attempt(cfg, holder, max_iter=None):
        try:
            from ai_agent import agent_loop
            holder['r'] = agent_loop(
                user_message=prep['agent_msg'], llm_config=cfg,
                file_name=current_file, max_iterations=max_iter or prep['max_iter'],
                session_id=session_id,
                progress_callback=_cb,
                fast=fast)   # ⚡快速档：终稿收紧预算并要求核心结论
        except Exception as e:
            logger.warning("流式 Agent 执行失败: %s", e)
            holder['r'] = {'ok': False, 'error': str(e)}
        finally:
            evq.put(None)  # 结束哨兵

    holder: Dict[str, Any] = {}
    _threading.Thread(target=_attempt, args=(prep['eff_cfg'], holder), daemon=True).start()
    _idle, _stalled = 0.0, False
    while True:
        try:
            evt = evq.get(timeout=HEARTBEAT_S)
        except _queue.Empty:
            _idle += HEARTBEAT_S
            if _idle >= _AI_STALL_LIMIT_S:
                # P1-4③ 看门狗：长时间无进展 → 主动收尾，避免 waitress 线程被永久占用
                _stalled = True
                logger.warning('深度分析超过 %.0fs 无进展，判定卡死并收尾', _AI_STALL_LIMIT_S)
                break
            # 心跳：长时间无事件（LLM 限流退避/工具执行中）时保活连接
            yield {'type': 'ping', 'data': {'elapsed_s': HEARTBEAT_S}}
            continue
        _idle = 0.0
        if evt is None:
            break
        # 终稿流式增量 → 提升为顶层 delta 事件：前端 core.js 据此边生成边渲染，
        # 长报告不再是"静默等待一次性出现"。（其余事件仍走 progress）
        if evt.get('type') == 'delta':
            yield {'type': 'delta', 'data': {'text': evt.get('text', '')}}
        else:
            yield {'type': 'progress', 'data': evt}

    if _stalled:
        yield {'type': 'done', 'data': _stalled_result(message, current_file)}
        return

    ans = _finalize_agent_result(holder.get('r'), message, llm_config, current_file, prep, depth,
                                 fast=fast)
    if not ans:
        # 输出残缺（限流 429 常见）→ 轻量化重试一轮（削减迭代预算，少耗时也能提升成功率），进度照常转发
        yield {'type': 'status', 'message': '首次结果不完整，正在重试一轮...'}
        holder2: Dict[str, Any] = {}
        _lighter_iter = 2 if isinstance(prep['max_iter'], int) and prep['max_iter'] > 2 else prep['max_iter']
        # 重试必须沿用 eff_cfg —— 它承载着快速档的 reasoning_effort='none' 与复杂度调温。
        # 此前传原始 llm_config，重试会悄悄退回"开思考"（快速模式反而更慢）。
        _threading.Thread(target=_attempt, args=(prep['eff_cfg'], holder2, _lighter_iter),
                          daemon=True).start()
        _idle = 0.0
        while True:
            try:
                evt = evq.get(timeout=HEARTBEAT_S)
            except _queue.Empty:
                _idle += HEARTBEAT_S
                if _idle >= _AI_STALL_LIMIT_S:
                    logger.warning('深取分析重试同样超过 %.0fs 无进展，收尾', _AI_STALL_LIMIT_S)
                    yield {'type': 'done', 'data': _stalled_result(message, current_file)}
                    return
                yield {'type': 'ping', 'data': {'elapsed_s': HEARTBEAT_S}}
                continue
            _idle = 0.0
            if evt is None:
                break
            if evt.get('type') == 'delta':
                yield {'type': 'delta', 'data': {'text': evt.get('text', '')}}
            else:
                yield {'type': 'progress', 'data': evt}
        ans = _finalize_agent_result(holder2.get('r'), message, llm_config, current_file, prep, depth,
                                     fast=fast)

    if ans:
        yield {'type': 'done', 'data': ans}
        return
    # 两轮 Agent 均失败（多为 LLM 限流/超时）：降级为确定性兜底报告，保证"非空"。
    # 有文件走数据驱动计算报告；无文件（原因/趋势/最新类）走联网聚合最新资料。
    _fb = _deterministic_fallback_report(message, current_file)
    if _fb:
        yield {'type': 'done', 'data': _fb}
        return
    # 连确定性兜底也拿不到数据时，给出诚实且可操作的提示（保留过程记录）。
    yield {'type': 'done', 'data': {
        'ok': True,
        'reply': '⚠️ 深度分析本轮未产出有效报告（多为 LLM 限流/超时所致）。\n\n'
                 '已完成的过程记录见上方「思考过程」面板；请稍后重试，或切换付费模型以避免限流。',
        'source': 'agent',
        'intent': {'action': 'analyze_data', 'params': {'file': current_file}, 'confidence': 0.9, 'raw_text': message},
        'plan': {'steps': [], 'context': self_context_summary()},
    }}


def stream_chat_message(message: str, llm_config: dict = None, current_file: str = None,
                        mode: str = 'think', images: list = None, session_id: str = None,
                        pipeline_models: dict = None, conversation_id: str = None):
    """
    SSE 流式处理对话消息：yield {'type':'status'|'delta'|'done', ...}
    - 识图（带图片）→ 多模态 LLM 流式描述/分析
    - 知识问答（think）→ LLM 流式综合回答
    - 快速模式知识问答 → 直接返回原文（一次 done）
    - 快速聊天 → LLM 流式简短回答
    - 联网回答 → 搜索 + LLM 流式
    - 分析请求 → Agent（非流式，一次 done，含建议操作）
    conversation_id: 多会话隔离键；非空时入口即切换当前会话（route cache / 记忆桶 /
                     last_file 一起跟着切，否则新会话会接着上一会话的上下文）。
    """
    from llm_client import chat_stream
    fast = mode == 'fast'
    if conversation_id:
        activate_chat_conversation(conversation_id)

    # 纯寒暄秒回（流式与同步同构）
    if _is_pure_greeting(message):
        _g = _greeting_payload()
        _g['intent']['raw_text'] = message
        yield {'type': 'done', 'data': _g}
        return
    _skills = _skills_context()  # 已启用 Skills（普通对话也注入，使 skill 生效）

    # ---------- 0.0 识图（多模态）：带图片优先走视觉模型 ----------
    if images:
        _blocked = _vision_egress_blocked(message)
        if _blocked:
            yield {'type': 'done', 'data': {
                'ok': False, 'reply': _blocked['reply'], 'source': 'vision',
                'llm_synthesized': False, 'mode': mode, 'intent': _blocked['intent'],
            }}
            return
        yield {'type': 'status', 'message': '正在识别图片...'}
        content = [{"type": "text", "text": message or "请描述这张图片的内容"}]
        for img in images[:4]:
            content.append({"type": "image_url", "image_url": {"url": img}})
        v_msgs = [
            {"role": "system", "content": "你是 data.tool 的多模态分析助手，具备图片识别能力。请根据用户要求描述或分析图片内容，用中文回答，简洁准确。"},
            {"role": "user", "content": content},
        ]
        parts = []
        try:
            for tok in chat_stream(v_msgs, llm_config, max_tokens=1000):
                parts.append(tok)
                yield {'type': 'delta', 'text': tok}
        except Exception as e:
            logger.warning("识图流式失败: %s", e)
        answer = ''.join(parts).strip()
        if not answer:
            answer = '⚠️ 当前模型不支持图片识别（识图需要支持视觉的多模态模型）。请在 AI 助手设置中切换支持视觉的模型后重试。'
        yield {'type': 'done', 'data': {
            'ok': True, 'reply': answer, 'source': 'vision', 'llm_synthesized': bool(parts),
            'mode': mode, 'intent': {'action': 'chat', 'params': {}, 'confidence': 0.9, 'raw_text': message},
        }}
        return

    # ---------- 0. 金融实时分析（估值/股价类问题优先取真实数据，跳过知识库通用回答） ----------
    # 深度分析请求（思考模式）不在此短路，放行到 Agent 链路做全维度报告（与同步路径一致）
    _fin_live = _finance_live_context(message)
    if not _fin_live and _references_recent_target(message):
        # 指代式追问（"那它现在估值呢"）：复用上一轮标的重新取真实数据
        _ls = ((_chat_manager.context or {}).get('last_stock') or {}).get('code')
        if _ls:
            _fin_live = _finance_live_context('', code=_ls)
    _fin_is_deep = (not fast) and _message_depth(message) == 'deep'
    if _fin_live and not _fin_is_deep:
        yield {'type': 'status', 'message': '正在获取实时估值与财务数据...'}
        sys_prompt = (
            "你是用户身边靠谱的数据分析与金融顾问。用户询问股票估值/股价是否合理，"
            "以下是系统自动拉取的实时数据（DCF 估值 + 财务比率），请基于这些真实数据简洁分析："
            "先给结论（低估/合理/高估），再列 2-3 个关键依据，引用数据里的具体数字。"
            "结尾必须注明：⚠️ 本结论为客观数据分析，仅供参考，不构成任何投资建议。"
            + _depth_style('balanced')
        )
        if _skills:
            sys_prompt = sys_prompt + "\n\n# 已加载技能（请遵循）\n" + _skills
        user_prompt = f"用户问题：{message}\n\n实时数据：\n{_fin_live['context']}"
        parts = []
        try:
            for tok in chat_stream([{'role': 'system', 'content': sys_prompt},
                                    {'role': 'user', 'content': user_prompt}], llm_config, max_tokens=600):
                parts.append(tok)
                yield {'type': 'delta', 'text': tok}
        except Exception as e:
            logger.warning("金融实时分析流式失败，回退数据文本: %s", e)
        answer = ''.join(parts).strip() or _fin_live['context']
        yield {'type': 'done', 'data': {
            'ok': True, 'reply': answer, 'source': 'finance_live',
            'llm_synthesized': bool(parts), 'mode': mode,
            'intent': {'action': 'chat', 'params': {}, 'confidence': 0.9, 'raw_text': message},
            'finance': {'code': _fin_live['code']},
        }}
        return

    # ---------- 1. 意图分流中间层（共享）+ 知识检索 ----------
    _intent_router, _norm_msg, _role_prompt = _router_context(message, llm_config, current_file)
    _fin_meta = _intent_router.get('finance') or {}
    # 智能运营分流（与同步路径同一入口，让路规则内置；命中即确定性应答，不进 LLM）
    _ops = try_ops_reply(message, current_file, fast=(mode == 'fast'))
    if _ops:
        yield {'type': 'done', 'data': _ops}
        return
    # P0-2 一致性：功能指引/排障类意图不走知识库抢答，交给下方 help 分支
    # P2 双轨收敛：与同步路径共用同一判定函数，杜绝两路漂移
    _kb_blocked_by_intent = _kb_should_yield(message, _intent_router, current_file)
    try:
        # 检索 + 合并 + 金融子意图抑制 → 集中在共享决策函数（消除双路径漂移）
        from knowledge_rag import format_knowledge_answer
        merged, is_kb, ent_is_kb = _joined_kb_hits(message, _norm_msg, _fin_meta)
        if (is_kb or ent_is_kb) and not _kb_blocked_by_intent:
            if fast:
                # 快速：直接原文，一次性返回
                yield {'type': 'done', 'data': {
                    'ok': True, 'reply': format_knowledge_answer(merged),
                    'source': 'knowledge', 'llm_synthesized': False, 'mode': mode,
                    'knowledge': [e.get('name', '') for e in merged[:5]],
                    'intent': {'action': 'knowledge', 'params': {}, 'confidence': 0.95, 'raw_text': message},
                }}
                return
            # 思考：LLM 流式综合
            yield {'type': 'status', 'message': '正在整理知识点回答...'}
            context_parts = []
            for i, h in enumerate(merged[:3], 1):
                context_parts.append(f"[{i}] {h.get('name', '')}：{(h.get('detail','') or h.get('desc','') or '')[:300]}")
            context = "\n\n".join(context_parts)
            _kb_depth = _intent_router.get('depth') or _message_depth(message)
            sys_prompt = (
                "你是用户身边靠谱的数据分析与金融顾问，知识面广、讲人话、有判断力。"
                "基于提供的知识点，用自己的话把问题讲清楚，并给出你的分析与建议。"
                + _HUMAN_STYLE +
                "不要编造知识点之外的事实；涉及政策、利率、税率时提醒以官方最新为准。"
                + _depth_style(_kb_depth)
            )
            if _role_prompt:
                sys_prompt = sys_prompt + "\n" + _role_prompt
            _expert_ctx = _build_expert_context(_norm_msg, depth=_kb_depth)
            if _expert_ctx:
                sys_prompt = sys_prompt + "\n" + _expert_ctx
            user_prompt = f"对话历史：\n{_recent_history_block(max_chars=120)}\n\n用户问题：{message}\n\n知识点参考：\n{context}\n\n请结合知识点与历史，简洁地回答用户问题。"
            try:
                parts = []
                for tok in chat_stream([{'role': 'system', 'content': sys_prompt},
                                        {'role': 'user', 'content': user_prompt}], llm_config, max_tokens=800):
                    parts.append(tok)
                    yield {'type': 'delta', 'text': tok}
                answer = ''.join(parts).strip()
            except Exception as e:
                logger.warning("流式知识回答失败，回退原文: %s", e)
                answer = format_knowledge_answer(merged)
            answer = kb_answer(answer, merged)
            yield {'type': 'done', 'data': {
                'ok': True, 'reply': answer, 'source': 'knowledge',
                'llm_synthesized': True, 'mode': mode,
                'knowledge': [e.get('name', '') for e in merged[:5]],
                'intent': {'action': 'knowledge', 'params': {}, 'confidence': 0.95, 'raw_text': message},
            }}
            return
    except Exception as e:
        logger.warning("知识点检索失败: %s", e)

    # ---------- 2. 意图识别 ----------
    result = _chat_manager.process_message(message, llm_config, current_file=current_file)
    # P0-2：派发 action 与同步路径一致，以 intent_router 判定为准，旧正则降为兜底
    _regex_action = (result.get('intent') or {}).get('action')
    action = _effective_action(message, _intent_router, _regex_action)
    if isinstance(result.get('intent'), dict):
        result['intent']['action'] = action

    # 模糊/低置信度意图 → 引导式追问（路由中间层）
    _clarity = _maybe_clarify(message, _intent_router, current_file)
    if _clarity:
        yield {'type': 'done', 'data': _clarity}
        return

    # 概念/方法型问题护栏（与同步路径一致）："什么是DID"不走"请上传文件"的分析流程
    # P0-3 扩展：方法论问法（"如何做好数据分析"）同样不走文件流程
    _method_like = (_is_methodology_question(message)
                    and not _has_analyzable_target(message)
                    and not _wants_deep_report(message))
    if action == 'analyze_data' and not current_file and (
            _is_concept_question(message, _intent_router) or _method_like):
        _c_ctx = _build_expert_context(message, depth=_depth_for_mode(_intent_router.get('depth') or 'balanced', fast))
        _c_role = (_c_ctx + "\n" + _role_prompt) if _c_ctx else _role_prompt
        _c_ans = (_fast_chat_answer(message, llm_config, role_prompt=_c_role) if fast
                  else _web_search_answer(message, llm_config, role_prompt=_c_role,
                                          depth=_depth_for_mode(_intent_router.get('depth'), fast)))
        if _c_ans:
            yield {'type': 'done', 'data': _c_ans}
            return

    # 分析类：Agent 深度分析（流式：计划/工具调用/耗时实时转发）
    if action in ('analyze_data', 'correlation', 'clean_data', 'train_model', 'create_chart'):
        file_mentioned = (result.get('intent', {}).get('params', {}) or {}).get('file') or current_file
        if not file_mentioned:
            # 金融路由子意图、明确分析目标、以指代延续上一轮、或要求完整报告（P0-3）→ 无文件也直接分析
            _fin_sub_s = (_intent_router.get('finance') or {}).get('subintent')
            if ((_fin_sub_s in _FIN_ROUTE_SUBINTENTS) or _has_analyzable_target(message)
                    or _needs_structured_research(message)
                    or _references_recent_target(message)
                    or _wants_deep_report(message)):
                got = None
                # 高价值显式请求（研究报告/全面分析/尽调）→ 三阶段多 Agent 协作管线
                _use_pipe = (not fast)
                try:
                    from agent_pipeline import should_use_pipeline
                    _use_pipe = _use_pipe and should_use_pipeline(message)
                except Exception:
                    _use_pipe = False
                if _use_pipe:
                    from agent_pipeline import run_pipeline_stream
                    for evt in run_pipeline_stream(message, llm_config, None, session_id=session_id,
                                                   pipeline_models=pipeline_models):
                        if evt['type'] == 'done':
                            got = evt['data']
                        else:
                            yield evt
                    if got:
                        yield {'type': 'done', 'data': got}
                        return
                for evt in _stream_agent_analysis(message, llm_config, None,
                                                  depth=_intent_router.get('depth'),
                                                  session_id=session_id, fast=fast):
                    if evt['type'] == 'done':
                        got = evt['data']
                    else:
                        yield evt
                if got:
                    yield {'type': 'done', 'data': got}
                    return
                fb = _no_file_analysis_fallback(message, llm_config,
                                                depth=_intent_router.get('depth') or 'deep',
                                                role_prompt=_intent_router.get('role_prompt') or '')
                if fb:
                    yield {'type': 'done', 'data': fb}
                    return
            yield {'type': 'done', 'data': {
                'ok': True,
                'reply': '📂 我注意到你想做数据分析，但当前还没有选择数据文件，我也还不知道要分析什么。\n\n'
                         '两种方式任选：\n'
                         '1. **有数据文件**：在顶部选择文件（.csv / .xlsx），然后对我说 **"帮我分析当前文件"**；\n'
                         '2. **没有文件也可以**：直接告诉我要分析的行业、股票或基金，例如 **"全面分析电器行业"**、'
                         '**"分析腾讯值不值得买"**，我会联网获取最新资料来分析。\n'
                         '如果想了解数据分析的概念和方法，可以问我："什么是数据分析"。',
                'source': 'rules',
                'intent': {'action': 'show_message', 'params': {}, 'confidence': 0.9, 'raw_text': message},
            }}
            return
        # ⚡快速档：同样走 agent（关闭思考 + 收紧迭代预算），仅跳过耗时的多 Agent 管线。
        # 修复：此前 `if not fast:` 把整段包住 → 快速模式带文件分析时直接落到函数末尾
        # `yield {'type':'done','data':result}`，而 result 是规则引擎原生结构（无 reply 字段，
        # 实测 reply_len=0），用户拿到的是空回复 + 一份未执行的 5 步计划。
        got = None
        _use_pipe = False
        if not fast:
            try:
                from agent_pipeline import should_use_pipeline
                _use_pipe = should_use_pipeline(message)
            except Exception:
                _use_pipe = False
        if _use_pipe:
            from agent_pipeline import run_pipeline_stream
            for evt in run_pipeline_stream(message, llm_config, file_mentioned,
                                           session_id=session_id,
                                           pipeline_models=pipeline_models):
                if evt['type'] == 'done':
                    got = evt['data']
                else:
                    yield evt
            if got:
                yield {'type': 'done', 'data': got}
                return
        for evt in _stream_agent_analysis(message, llm_config, file_mentioned,
                                          depth=_intent_router.get('depth'),
                                          session_id=session_id, fast=fast):
            if evt['type'] == 'done':
                got = evt['data']
            else:
                yield evt
        if got:
            yield {'type': 'done', 'data': got}
            return

    # 聊天类（含未命中知识库的 knowledge / 自然语言查数 nlq）：快速 → LLM 流式简短；思考 → 联网/直接回答
    if action in ('chat', 'greeting', 'help', 'knowledge', 'nlq'):
        # 本地兜底（问候/帮助/域外/计算/乱码）：逻辑集中在单一共享函数，消除双路径漂移
        _guard = _chat_guard_reply(message, action, _intent_router, result, current_file)
        if _guard:
            yield {'type': 'done', 'data': _guard}
            return
        # 双轨一致：与同步路径相同，nlq/analysis 需注入真实计算或数据摘要
        # （流式此前缺这一段，导致同样的问题在两条链路上拿到的上下文不同）
        _calc_extra = ""
        if current_file and _intent_router.get('intent') in ('nlq', 'analysis'):
            _calc_block = _compute_note(message, current_file)
            if _calc_block:
                _calc_extra = _calc_block
            elif action == 'nlq':
                _brief = _nlq_data_brief(current_file)
                if _brief:
                    _calc_extra = _brief
        if fast:
            yield {'type': 'status', 'message': '快速回答中...'}
            sys_prompt = ("你是用户的智能助手，回答要快而准。用中文、口语化、**不超过 3 句话**，直接回答问题，不要铺垫。不知道的如实说不知道，别编造。")
            if _role_prompt:
                sys_prompt = sys_prompt + "\n" + _role_prompt
            _expert_ctx = _build_expert_context(message)
            if _expert_ctx:
                sys_prompt = sys_prompt + "\n" + _expert_ctx
            user_prompt = (f"对话历史：\n{_recent_history_block(max_chars=120)}\n\n"
                           + (f"{_calc_extra}\n\n" if _calc_extra else "")
                           + f"用户问题：{message}\n\n请简短回答。")
            try:
                parts = []
                for tok in chat_stream([{'role': 'system', 'content': sys_prompt},
                                        {'role': 'user', 'content': user_prompt}], llm_config, max_tokens=250):
                    parts.append(tok)
                    yield {'type': 'delta', 'text': tok}
                content = ''.join(parts).strip()
            except Exception as e:
                logger.warning("快速流式回答失败: %s", e)
                content = _fast_chat_fallback(message)
            yield {'type': 'done', 'data': {
                'ok': True, 'reply': content, 'source': 'fast', 'mode': mode,
                'intent': {'action': 'chat', 'params': {}, 'confidence': 0.7, 'raw_text': message},
            }}
            return
        else:
            # 时事/市场/原因类问题：走 Agent 深度链路（web_search 取数 + 分维度结构化报告 + 思考可见），
            # 满足「完整有条理 + 联网必须用上 + 思考全部展示」的要求，而非精简检索回答。
            if not current_file and _needs_structured_research(message):
                got = None
                for evt in _stream_agent_analysis(message, llm_config, None,
                                                  depth=_intent_router.get('depth') or 'deep',
                                                  session_id=session_id):
                    if evt['type'] == 'done':
                        got = evt['data']
                    else:
                        yield evt
                if got:
                    yield {'type': 'done', 'data': got}
                    return
            # P1 闸门：不需要最新/外部信息时直接回答，不联网（避免"请继续/1+1"无谓搜索）
            if not _needs_web_search(message, _intent_router):
                yield {'type': 'status', 'message': '正在生成回答...'}
                _d_depth = _intent_router.get('depth') or 'balanced'
                sys_prompt = (_domain_system_prompt(message) + _depth_style(_d_depth)
                              + "不确定的信息如实说明，不要编造事实与数字。")
                if _role_prompt:
                    sys_prompt = sys_prompt + "\n" + _role_prompt
                _expert_ctx = _build_expert_context(message, depth=_d_depth)
                if _expert_ctx:
                    sys_prompt = sys_prompt + "\n" + _expert_ctx
                if _skills:
                    sys_prompt = sys_prompt + "\n\n# 已加载技能（若任务相关请遵循）\n" + _skills
                user_prompt = (f"对话历史：\n{_recent_history_block(max_chars=150)}\n\n"
                               + (f"{_calc_extra}\n\n" if _calc_extra else "")
                               + f"用户问题：{message}\n\n请直接回答。")
                parts_d = []
                try:
                    for tok in chat_stream([{'role': 'system', 'content': sys_prompt},
                                            {'role': 'user', 'content': user_prompt}],
                                           llm_config,
                                           max_tokens=(800 if _d_depth == 'deep' else 400)):
                        parts_d.append(tok)
                        yield {'type': 'delta', 'text': tok}
                except Exception as e:
                    logger.warning("流式直接回答失败: %s", e)
                content_d = ''.join(parts_d).strip() or _fast_chat_fallback(message)
                yield {'type': 'done', 'data': {
                    'ok': True, 'reply': content_d, 'source': 'llm', 'mode': mode,
                    'intent': {'action': 'chat', 'params': {}, 'confidence': 0.8, 'raw_text': message},
                }}
                return
            # 思考：联网搜索 + 流式
            yield {'type': 'status', 'message': '正在联网搜索资料...'}
            try:
                from web_search import web_search
                res = web_search(message, top_k=5)
                if not res.get("ok") or not res.get("results"):
                    yield {'type': 'done', 'data': {
                        'ok': True,
                        'reply': f'⚠️ 联网搜索暂时不可用：{res.get("error", "") or "未找到相关网页"}。可稍后重试，或换个说法再问。',
                        'source': 'web',
                        'intent': {'action': 'chat', 'params': {}, 'confidence': 0.8, 'raw_text': message},
                    }}
                    return
                items = res['results']
                sources = [{'title': it['title'][:60], 'url': it['url']} for it in items[:5]]
                snippets = "\n".join(f"{i+1}. {it['title']}\n   链接: {it['url']}\n   摘要: {it['snippet']}" for i, it in enumerate(items))
                yield {'type': 'status', 'message': '正在生成回答...'}
                sys_prompt = _domain_system_prompt(message) + "回答简洁，300 字以内，直接给结论。"
                if _role_prompt:
                    sys_prompt = sys_prompt + "\n" + _role_prompt
                _expert_ctx = _build_expert_context(message, depth=_depth_for_mode(_intent_router.get('depth') or 'balanced', fast))
                if _expert_ctx:
                    sys_prompt = sys_prompt + "\n" + _expert_ctx
                if _skills:
                    sys_prompt = sys_prompt + "\n\n# 已加载技能（若任务相关请遵循）\n" + _skills
                user_prompt = f"对话历史：\n{_recent_history_block(max_chars=150)}\n\n用户问题：{message}\n\n网页搜索结果：\n{snippets}"
                parts = []
                for tok in chat_stream([{'role': 'system', 'content': sys_prompt},
                                        {'role': 'user', 'content': user_prompt}], llm_config, max_tokens=1000):
                    parts.append(tok)
                    yield {'type': 'delta', 'text': tok}
                content = ''.join(parts).strip() or '抱歉，我没能组织出有效回答，请换个说法再问。'
                yield {'type': 'done', 'data': {
                    'ok': True, 'reply': content, 'source': 'web', 'sources': sources, 'mode': mode,
                    'intent': {'action': 'chat', 'params': {}, 'confidence': 0.8, 'raw_text': message},
                }}
                return
            except Exception as e:
                logger.warning("联网流式问答失败: %s", e)

    # ---------- 3. 其他（rules）→ 一次 done ----------
    yield {'type': 'done', 'data': result}


def _fast_chat_fallback(message: str) -> str:
    """快速模式聊天 LLM 不可用时的轻量兜底"""
    q = message.lower().strip()
    if any(w in q for w in ('你好', 'hi', 'hello', '嗨', '哈喽')):
        return '你好呀！我是你的数据分析助手 👋 可以说"帮我分析当前文件"，或直接问我金融/税务问题。'
    if any(w in q for w in ('帮助', '怎么用', '功能', 'help', '说明')):
        return '我可以：📊 分析数据、📈 画图、🔗 相关性分析、🧹 数据清洗、📚 解答金融/税务知识。'
    if len(q) <= 6:
        return f'你说的是「{message}」吗？具体想了解什么？也可以试试"帮我分析当前文件"。'
    return '这是快速模式回答（⚡）。如果想让我联网查资料或深度分析，切换到 🧠 思考模式再问一次。'


_ent_kb_fp = None      # 企业知识库条目的指纹（len+首尾id）
_ent_kb_stamp = 0.0    # 最近一次加载企业库的时间戳
_ent_kb_entries = []   # 缓存的条目列表


def _enterprise_entries() -> list:
    """读取企业知识库并做索引缓存（按指纹 + 时限失效），避免每次查询全量 .all() + 重建索引"""
    global _ent_kb_fp, _ent_kb_stamp, _ent_kb_entries
    try:
        from models import KnowledgeDoc
        rows = KnowledgeDoc.query.with_entities(
            KnowledgeDoc.id, KnowledgeDoc.title, KnowledgeDoc.content).all()
        ids = [r[0] for r in rows] if rows else []
        fp = (len(ids), ids[0] if ids else None, ids[-1] if ids else None)
        if fp == _ent_kb_fp and (time.time() - _ent_kb_stamp) < 60:
            return _ent_kb_entries
        # 去掉 content[:800] 强截断（P1-4）：块级检索已能处理长文；desc 只做注入时的精简引导
        _ent_kb_entries = [{
            'name': (r[1] or '未命名'),
            'desc': (r[2] or '')[:180],
            'detail': r[2] or '',
            'tab': '企业知识库',
            'category': 'enterprise',
            'level': 'core',  # 企业自有文档视为权威核心，注入时优先
        } for r in rows]
        _ent_kb_fp = fp
        _ent_kb_stamp = time.time()
        return _ent_kb_entries
    except Exception as e:
        # P2：无 Flask app context（离线诊断/脚本/单测）时属预期，静默降级不刷屏；
        # 仅真正的数据库异常才告警，避免日志噪声掩盖真实问题。
        _msg = str(e)
        if 'application context' in _msg or 'working outside' in _msg or 'RuntimeError' in type(e).__name__:
            logger.debug("企业知识库不可用（无 app context，已跳过）: %s", _msg)
        else:
            logger.warning("企业知识库读取失败: %s", e)
        return []


def _search_enterprise_knowledge(query: str, top_k: int = 3) -> list:
    """检索企业知识库（数据库 KnowledgeDoc），返回 [(score, entry)]。
    走通用块级混合检索 + 缓存索引；混合不可用时退回词面 + TF-IDF。"""
    try:
        from knowledge_rag import rank_entries, search_over_entries
        entries = _enterprise_entries()
        if not entries:
            return []
        try:
            res = rank_entries(query, entries, top_k=top_k)
            if res:
                return res
        except Exception:
            pass
        return search_over_entries(query, entries, top_k=top_k)
    except Exception as e:
        logger.warning("企业知识库检索失败: %s", e)
        return []


def _recent_history_block(max_chars: int = 150) -> str:
    """拼接最近多轮对话历史（跳过当前句），供 LLM 衔接上下文；无历史返回提示语。

    多会话（2026-09-20）：历史必须**按当前会话过滤**。`_chat_manager.history` 是
    全局扁平列表（`conversation_id` 只是消息上的标签），不过滤就会把 A 会话的内容
    当作 B 会话的上下文喂给模型 —— 表现为"新对话里 AI 莫名接着上一个话题"。
    """
    hist = _chat_manager.history
    cid = getattr(_chat_manager, 'active_conversation_id', 'legacy') or 'legacy'
    scoped = [m for m in hist
              if (getattr(m, 'conversation_id', 'legacy') or 'legacy') == cid]
    lines = []
    for m in scoped[-7:]:
        if m is scoped[-1]:
            continue
        # 提示词复述（元结构泄漏）不入上下文：喂回去后模型会看到"自己上次这么答"，
        # 下一轮继续复述 —— 实测会越滚越顽固，必须在这里切断。
        if m.role != 'user' and looks_like_prompt_echo(m.content):
            continue
        role = '用户' if m.role == 'user' else '助手'
        lines.append(f"{role}: {m.content[:max_chars]}")
    return "\n".join(lines) if lines else "（无历史，这是首轮提问）"


def _rag_answer(message: str, hits: list, llm_config: dict = None, role_prompt: str = '',
                depth: str = 'balanced') -> str:
    """基于检索到的知识，用 LLM 综合、归纳生成回答（'有自己的思考'）；LLM 不可用返回 None。

    depth 感知：deep 允许展开（讲透概念 + 举例 + 常见误区），balanced/concise 收紧。"""
    if not hits:
        return None
    try:
        from llm_client import chat
    except Exception:
        return None

    context_parts = []                             # 注入侧带引用编号，使回答可溯源
    for h in hits[:3]:
        name = h.get('name', '')
        detail = (h.get('detail', '') or h.get('desc', '') or '')[:300]
        context_parts.append(f"[{cite_id(h)}] {name}：{detail}")
    context = "\n\n".join(context_parts)

    hist_block = _recent_history_block(max_chars=120)
    max_tokens = 700 if depth == 'deep' else (120 if depth == 'concise' else 300)

    sys_prompt = (
        "你是用户身边靠谱的数据分析与金融顾问，知识面广、讲人话、有判断力。"
        "基于提供的知识点，用自己的话把问题讲清楚，并给出你的分析与建议。"
        "引用某条知识点时，在句末标注其方括号编号（如 [KB-tax-1a2b]），编号只能取自提供的条目。"
        + _HUMAN_STYLE +
        "不要编造知识点之外的事实；涉及政策、利率、税率时提醒以官方最新为准。"
        + _depth_style(depth)
    )
    if depth == 'deep':
        sys_prompt += "知识点没覆盖的部分明确说明，可补充通识但不得虚构具体数字或出处。"
    if role_prompt:
        sys_prompt = sys_prompt + "\n" + role_prompt
    user_prompt = (
        f"对话历史：\n{hist_block}\n\n"
        f"用户问题：{message}\n\n"
        f"知识点参考：\n{context}\n\n"
        f"请结合知识点与历史回答用户问题。"
    )
    try:
        _cfg, _mt = _shallow_llm_config(llm_config, max_tokens=max_tokens)
        resp = chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            _cfg,
            max_tokens=_mt,
        )
        content = (resp.get('content') or '').strip()
        return content or None
    except Exception as e:
        logger.warning("RAG 综合回答失败: %s", e)
        return None


# 人性化回答风格（消除"机器人感"，让 AI 像资深顾问一样聊天）
# 由 prompts.py 集中管理，支持 data/prompt_versions.json 版本化覆盖
from prompts import get_prompt

_HUMAN_STYLE = get_prompt('human_style')


def _domain_system_prompt(message: str) -> str:
    """按问题领域（金融/银行/税务）注入专家人设与免责，其余用通用问答人设"""
    from prompts import resolve_domain_prompt
    return resolve_domain_prompt(message)


def _direct_answer(message: str, llm_config: dict = None, role_prompt: str = '',
                   depth: str = 'balanced', extra_context: str = '') -> Dict[str, Any]:
    """不联网的直接 LLM 回答（P1 闸门命中"无需联网"时使用）：闲聊/算术/概念直觉题。

    与 _web_search_answer 结构一致（返回 done 数据结构），只是省去搜索环节——
    避免"请继续/1+1/今天天气"这类问题无谓联网，同时保留 depth 感知的篇幅控制。
    """
    try:
        from llm_client import chat
        hist_block = _recent_history_block(max_chars=150)
        max_tokens = 800 if depth == 'deep' else (150 if depth == 'concise' else 400)
        sys_prompt = (_domain_system_prompt(message)
                      + _depth_style(depth)
                      + "不确定的信息如实说明，不要编造事实与数字。")
        if role_prompt:
            sys_prompt = sys_prompt + "\n" + role_prompt
        user_prompt = (
            f"对话历史：\n{hist_block}\n\n"
            f"用户问题：{message}\n\n"
            + (f"后端计算/护栏上下文：\n{extra_context}\n\n" if extra_context else "")
            + "请直接回答。"
        )
        _cfg, _mt = _shallow_llm_config(llm_config)
        resp = chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            _cfg,
            max_tokens=max_tokens,
        )
        content = (resp.get("content") or "").strip()
        if content:
            return {
                'ok': True,
                'reply': content,
                'source': 'llm',
                'intent': {'action': 'chat', 'params': {}, 'confidence': 0.8, 'raw_text': message},
                'plan': {'steps': [], 'context': _chat_manager.context},
            }
    except Exception as e:
        logger.warning("直接回答失败: %s", e)
    # 失败回退到轻量兜底（不联网）
    return _fast_chat_answer(message, llm_config, role_prompt=role_prompt,
                             extra_context=extra_context)


def _web_search_answer(message: str, llm_config: dict = None, role_prompt: str = '',
                       depth: str = 'balanced', extra_context: str = '') -> Dict[str, Any]:
    """联网搜索 + LLM 生成回答（带多轮历史上下文、来源链接）；失败返回友好提示。

    depth 感知：deep 请求允许充分展开（结论→依据→反方视角→边界），
    balanced/concise 收紧篇幅——不再对深度问题硬压 300 字。"""
    try:
        from web_search import web_search
        from llm_client import chat

        res = web_search(message, top_k=5)
        if not res.get("ok") or not res.get("results"):
            err = res.get('error', '') or '未找到相关网页'
            return {
                'ok': True,
                'reply': f'⚠️ 联网搜索暂时不可用：{err}。可稍后重试，或换个说法再问。',
                'source': 'web',
                'intent': {'action': 'chat', 'params': {}, 'confidence': 0.8, 'raw_text': message},
                'plan': {'steps': [], 'context': _chat_manager.context},
            }

        items = res['results']
        sources = [{'title': it['title'][:60], 'url': it['url']} for it in items[:5]]
        snippets = "\n".join(
            f"{i + 1}. {it['title']}\n   链接: {it['url']}\n   摘要: {it['snippet']}"
            for i, it in enumerate(items))

        hist_block = _recent_history_block(max_chars=150)
        max_tokens = 900 if depth == 'deep' else (150 if depth == 'concise' else 400)

        # 系统提示只放角色指令；搜索结果与历史放 user 侧，避免网页内容注入指令
        sys_prompt = (_domain_system_prompt(message)
                      + _depth_style(depth)
                      + "关键数字必须来自搜索结果或用户提供的上下文，搜索结果没覆盖的数字不要给。")
        if role_prompt:
            sys_prompt = sys_prompt + "\n" + role_prompt
        user_prompt = (
            f"对话历史：\n{hist_block}\n\n"
            f"用户问题：{message}\n\n"
            + (f"后端计算/护栏上下文：\n{extra_context}\n\n" if extra_context else "")
            + f"网页搜索结果：\n{snippets}"
        )
        _cfg, _mt = _shallow_llm_config(llm_config)
        resp = chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            _cfg,
            max_tokens=max_tokens,
        )
        content = (resp.get("content") or "").strip()
        if not content:
            content = '抱歉，我没能组织出有效回答，请换个说法再问。'
        return {
            'ok': True,
            'reply': content,
            'sources': sources,
            'source': 'web',
            'intent': {'action': 'chat', 'params': {}, 'confidence': 0.8, 'raw_text': message},
            'plan': {'steps': [], 'context': _chat_manager.context},
        }
    except Exception as e:
        logger.warning("联网问答失败: %s", e)
        return {
            'ok': True,
            'reply': f'⚠️ 联网问答暂时不可用（{e}），请稍后重试。',
            'source': 'web',
            'intent': {'action': 'chat', 'params': {}, 'confidence': 0.8, 'raw_text': message},
            'plan': {'steps': [], 'context': _chat_manager.context},
        }





def _depth_for_mode(depth, fast: bool = False) -> str:
    """快速模式限制思考深度（2026-09-22）：不得进入 deep，最多 balanced。"""
    d = (depth or "balanced").lower()
    if fast and d == "deep":
        return "balanced"
    return d if d in ("fast", "balanced", "deep") else "balanced"


def _shallow_llm_config(llm_config: dict = None, max_tokens: int = 400) -> tuple:
    """轻量/快速 LLM 配置：关 reasoning 系思考 + 小 token 预算。

    目的（2026-09-22）：快速模式/直答/知识综合等场景，思考深度不是卖点，
    却能贡献数秒～数十秒延迟。仅在 provider 显式支持时传 reasoning_effort。
    """
    cfg = dict(llm_config or {})
    prov = str(cfg.get('provider') or 'sensenova').lower()
    if prov in ('sensenova', 'minimax', 'deepseek', 'zhipu'):
        cfg['reasoning_effort'] = 'none'
    # 轻量路径不用高预算，避免模型写长文
    if not max_tokens or int(max_tokens) > 800:
        max_tokens = 400
    return cfg, max_tokens


def _fast_chat_answer(message: str, llm_config: dict = None, role_prompt: str = '',
                      extra_context: str = '') -> Dict[str, Any]:
    """快速模式聊天：不联网搜索，LLM 简短回答（≤3 句）；LLM 不可用给轻量兜底，保证秒级响应"""
    try:
        from llm_client import chat
        sys_prompt = (
            "你是用户的智能助手。用中文口语化回答，不超过3句，直接给结论，不要铺垫、"
            "不要输出任何思考过程。不知道就说不知道。纯寒暄就一句话回招呼。"
        )
        if role_prompt:
            sys_prompt = sys_prompt + "\n" + role_prompt
        user_prompt = (
            f"对话历史：\n{_recent_history_block(max_chars=120)}\n\n"
            f"用户问题：{message}\n\n"
            + (f"后端计算/护栏上下文：\n{extra_context}\n\n" if extra_context else "")
            + "请简短回答。"
        )
        # 轻聊/快答：关思考 + 小预算，避免 reasoning 系模型长思考（2026-09-22）
        _cfg, _mt = _shallow_llm_config(llm_config, max_tokens=300)
        resp = chat(
            [{"role": "system", "content": sys_prompt}, {"role": "user", "content": user_prompt}],
            _cfg,
            max_tokens=_mt,
        )
        content = (resp.get('content') or '').strip()
        if content:
            return {
                'ok': True,
                'reply': content,
                'source': 'fast',
                'mode': 'fast',
                'intent': {'action': 'chat', 'params': {}, 'confidence': 0.7, 'raw_text': message},
                'plan': {'steps': [], 'context': _chat_manager.context},
            }
    except Exception as e:
        logger.warning("快速回答失败: %s", e)

    # 轻量兜底：不联网、不依赖 LLM，按意图给一句话回答
    q = message.lower().strip()
    if any(w in q for w in ('你好', 'hi', 'hello', '嗨', '哈喽')):
        reply = '你好呀！👋 我是你的数据助手，有什么数据或金融分析需要帮忙的吗？'
    elif any(w in q for w in ('帮助', '怎么用', '功能', 'help', '说明')):
        reply = '我可以：📊 分析数据、📈 画图、🔗 相关性分析、🧹 数据清洗、📚 解答金融/税务知识。输入问题即可，切到思考模式可联网查最新政策。'
    elif len(q) <= 6:
        reply = f'你说的是「{message}」吗？具体想了解什么？也可以试试"帮我分析当前文件"。'
    else:
        reply = ('这是快速模式回答（⚡）。如果你想让我联网查资料或深度分析，点击输入框上方的 🧠 切换到思考模式再问一次。')
    return {
        'ok': True,
        'reply': reply,
        'source': 'fast',
        'mode': 'fast',
        'intent': {'action': 'chat', 'params': {}, 'confidence': 0.7, 'raw_text': message},
        'plan': {'steps': [], 'context': _chat_manager.context},
    }




_GREET_WORDS = ("你好", "您好", "你好呀", "你好！", "hi", "hello", "hey", "嗨", "哈喽", "在吗", "在么")
_GREET_REPLY = "你好呀！👋 我是你的数据助手，有什么数据或金融分析需要帮忙的吗？"


def _is_pure_greeting(message: str) -> bool:
    """纯寒暄（无业务诉求）→ 本地秒回，禁止进 LLM/知识库/专家约束。"""
    q = (message or "").strip().lower()
    if not q or len(q) > 12:
        return False
    if any(k in q for k in ("分析", "数据", "文件", "股票", "基金", "帮我", "查询", "计算", "报告", "清洗")):
        return False
    q2 = q.strip("。！!？?~ 　")
    return any(w in q2 for w in _GREET_WORDS) or q2 in _GREET_WORDS


def _greeting_payload() -> dict:
    return {
        "ok": True,
        "reply": _GREET_REPLY,
        "source": "fast",
        "mode": "fast",
        "intent": {"action": "greeting", "params": {}, "confidence": 0.95, "raw_text": ""},
        "plan": {"steps": [], "context": self_context_summary()},
    }



def self_context_summary() -> Dict[str, Any]:
    return _chat_manager.context

def _normalize_conversation_id(conversation_id: str = None) -> str:
    """会话 ID 只作为内存分组键使用，限制长度和字符以避免脏数据。"""
    cid = str(conversation_id or 'legacy').strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', cid):
        return 'legacy'
    return cid


def activate_chat_conversation(conversation_id: str = None) -> str:
    """切换当前会话，并为不同会话隔离临时上下文（last_file/last_action 不串台）。

    P1-2：同时切换 conversation_memory 的短期记忆桶 —— 旧实现只隔离了
    `_chat_manager.context`，而 `conversation_memory.short_term` 是进程级单例，
    导致切会话后首句「然后呢」被续接成上一会话的意图（且不触发澄清）。
    """
    cid = _normalize_conversation_id(conversation_id)
    previous = getattr(_chat_manager, 'active_conversation_id', 'legacy')
    _chat_manager._conversation_contexts[previous] = _chat_manager.context
    _chat_manager.active_conversation_id = cid
    _chat_manager.context = _chat_manager._conversation_contexts.setdefault(
        cid, {'last_file': None, 'last_action': None}
    )
    # 同步切换语义记忆桶（指代消解/DST 续接据此取历史）
    try:
        from conversation_memory import get_memory
        get_memory().set_active_conversation(cid)
    except Exception as e:
        logger.debug('切换会话记忆桶失败（忽略）: %s', e)
    return cid


def record_chat_exchange(user_content: str, assistant_content: str,
                         conversation_id: str = None) -> None:
    """把一次完整问答保存到所属会话；兼容 process_message 已先保存用户消息的路径。"""
    cid = activate_chat_conversation(conversation_id)
    with _chat_manager._history_lock:
        last = _chat_manager.history[-1] if _chat_manager.history else None
        user_already_saved = bool(
            last and last.role == 'user' and last.content == user_content
            and getattr(last, 'conversation_id', 'legacy') == cid
        )
        if not user_already_saved:
            _chat_manager.history.append(ChatMessage(
                role='user', content=user_content, conversation_id=cid
            ))
        if assistant_content and looks_like_prompt_echo(assistant_content):
            # P0-3：复述类回答不入历史。用户消息照记，只是不把这条"污染源"存下来，
            # 否则每轮都会被 _recent_history_block 喂回模型，形成自我强化。
            logger.warning('跳过写入"提示词复述"回答（避免自我强化）: %s',
                           str(assistant_content)[:60])
            assistant_content = ''
        if assistant_content:
            _chat_manager.history.append(ChatMessage(
                role='assistant', content=assistant_content, conversation_id=cid
            ))
        _chat_manager._trim_history_memory()
        # 强制落盘：对话记录是用户可见资产，不能依赖 2 秒节流 + atexit 兜底
        # （进程被强杀时最后一条会丢）。一轮问答间隔通常远大于 2 秒，
        # 该调用的实际写入频率仍是一轮一次，开销可忽略。
        _chat_manager.flush_history()


def get_chat_history(conversation_id: str = None) -> List[Dict]:
    """返回历史消息；指定 conversation_id 时只返回该会话（None=全部，向后兼容）。

    指定会话时**以归档为准**：主存只落最近 100 条，重度使用后老会话会被整段截断，
    只有归档留着完整内容 —— 否则"点回旧会话"会看到空白。
    再把"内存有、归档还没有"的增量补到末尾（归档写盘失败时的兜底）。
    两个来源统一输出 `{role, content, timestamp, conversation_id}`，形状与原契约一致。
    """
    cid = _normalize_conversation_id(conversation_id) if conversation_id else None
    mem = [{'role': m.role, 'content': m.content, 'timestamp': m.timestamp,
            'conversation_id': getattr(m, 'conversation_id', 'legacy')}
           for m in list(_chat_manager.history)  # P1-9：快照迭代
           if cid is None or getattr(m, 'conversation_id', 'legacy') == cid]
    if cid is None:
        return mem
    try:
        import chat_archive as _ca
        archived = [{'role': a.get('role'), 'content': a.get('content') or '',
                     'timestamp': a.get('timestamp') or '', 'conversation_id': cid}
                    for a in _ca.load_conversation(cid)]
    except (Exception, SystemExit):
        archived = []
    if not archived:
        return mem
    # 去重键用 (role, content, timestamp)：两边 timestamp 都是 HH:MM 口径
    seen = {(a['role'], a['content'], a['timestamp']) for a in archived}
    out = list(archived)
    for m in mem:
        if (m['role'], m['content'], m['timestamp']) in seen:
            continue
        out.append(m)
    return out


def _memory_conversation_summaries() -> List[Dict]:
    """把内存里的平铺消息聚合为会话摘要（归档不可用时的兜底路径）。"""
    grouped: Dict[str, Dict[str, Any]] = {}
    for index, m in enumerate(list(_chat_manager.history)):
        cid = getattr(m, 'conversation_id', 'legacy') or 'legacy'
        item = grouped.setdefault(cid, {
            'id': cid, 'title': '历史对话', 'preview': '', 'timestamp': '',
            'message_count': 0, 'days': [], '_last_index': index,
        })
        if m.role == 'user' and item['title'] == '历史对话' and m.content.strip():
            item['title'] = m.content.strip()[:36]
        if m.content.strip():
            item['preview'] = m.content.strip()[:80]
        item['timestamp'] = m.timestamp
        item['message_count'] += 1
        item['_last_index'] = index
    result = sorted(grouped.values(), key=lambda x: x['_last_index'], reverse=True)
    for item in result:
        item.pop('_last_index', None)
    return result


def _good_conversation_title(title: Any, cid: str) -> str:
    """过滤掉没有信息量的"标题"：空串、等于会话 id（'legacy' 之类）、通用兜底词。"""
    t = str(title or '').strip()
    if not t or t == cid or t == '历史对话':
        return ''
    return t


def get_chat_conversations() -> List[Dict]:
    """会话摘要列表（最近活动在前）。

    数据来源以**归档**为准：主存只有最近 100 条、老会话会被截断，归档永不截断。

    排序策略是两段拼接，而不是单纯按时间戳排：
      1) **内存里还能看到的会话** —— 按内存位置排序（精确反映"谁最后被写入"）；
      2) **只在归档里的会话** —— 按归档 last_ts 降序追加。
    为什么必须这样：归档里的时间戳只到秒/分钟级，同一秒（甚至同一分钟）内碰过的
    多个会话会**并列**，只按 ts 排就会退化成文件名序（实测把 chat_two 排到了
    chat_one 后面）。而主存截断丢掉的永远是**最老**的消息，所以"还在内存里的会话"
    必然比"只剩归档的会话"更近 —— 两段拼接天然保持全局时间序。
    """
    try:
        import chat_archive as _ca
        archived: Dict[str, Dict[str, Any]] = {
            str(c.get('id')): c for c in _ca.list_conversations() if c.get('id')
        }
    except (Exception, SystemExit):
        archived = {}
    out: List[Dict] = []
    seen = set()
    for c in _memory_conversation_summaries():      # 已按最近活动在前
        cid = c['id']
        seen.add(cid)
        merged = archived.get(cid)
        if merged:
            # 计数/日期集合以归档为准（跨天合并、永不截断）；排序位置仍来自内存
            item = dict(merged)
            # 标题优先用归档的开场问题，但它也可能是没有信息量的兜底值 → 回退内存
            item['title'] = (_good_conversation_title(item.get('title'), cid)
                             or _good_conversation_title(c.get('title'), cid)
                             or '历史对话')
            if not item.get('days'):
                item['days'] = list(c.get('days') or [])
            out.append(item)
        else:
            c['title'] = _good_conversation_title(c.get('title'), cid) or '历史对话'
            out.append(c)
    rest = [c for cid, c in archived.items() if cid not in seen]
    rest.sort(key=lambda x: str(x.get('last_ts') or ''), reverse=True)
    out.extend(rest)
    return out


def create_chat_conversation() -> Dict[str, Any]:
    """新建会话：生成合法 id、切为当前会话，返回会话摘要。

    id 必须落在 `_normalize_conversation_id` 的白名单 `[A-Za-z0-9_-]{1,80}` 内，
    否则会被静默降级成 'legacy'，新会话全部挤进同一个桶。
    追加随机后缀避免同一毫秒内连点生成同 id。
    """
    cid = _normalize_conversation_id(f'chat-{int(time.time() * 1000)}-{os.urandom(3).hex()}')
    activate_chat_conversation(cid)
    return {'id': cid, 'title': '新对话', 'preview': '', 'timestamp': '',
            'message_count': 0, 'days': [], 'active': True}


def clear_chat_history(conversation_id: str = None):
    with _chat_manager._history_lock:
        if conversation_id:
            cid = _normalize_conversation_id(conversation_id)
            _chat_manager.history[:] = [
                m for m in _chat_manager.history
                if getattr(m, 'conversation_id', 'legacy') != cid
            ]
            _chat_manager._conversation_contexts.pop(cid, None)
            if getattr(_chat_manager, 'active_conversation_id', 'legacy') == cid:
                _chat_manager.active_conversation_id = 'legacy'
                _chat_manager.context = _chat_manager._conversation_contexts.setdefault(
                    'legacy', {'last_file': None, 'last_action': None}
                )
        else:
            _chat_manager.history.clear()
            _chat_manager.context = {'last_file': None, 'last_action': None}
            _chat_manager.active_conversation_id = 'legacy'
            _chat_manager._conversation_contexts = {'legacy': _chat_manager.context}
        # 必须强制落盘：_save_history() 带 2 秒节流（P2-6），清空发生在刚保存过的
        # 2 秒窗口内时会被静默跳过 —— 内存已空而文件仍留旧记录，重启后"复活"，
        # 且 /api/assistant/history 返回空与磁盘内容不一致。
        # merge_disk=False：清空后不能再从磁盘把内容合并回来。
        _chat_manager.flush_history(merge_disk=False)
        # 本地归档同步移除：归档是用户可见的文件夹，界面删了文件还在会被当成"没删掉"
        try:
            import chat_archive as _ca
            _ca.purge(conversation_id=(
                _normalize_conversation_id(conversation_id) if conversation_id else None))
        except (Exception, SystemExit) as e:
            logger.debug('聊天归档清理失败（忽略）: %s', e)


def delete_chat_conversation(conversation_id: str) -> int:
    """只删除指定会话，不影响其他最近对话；返回删除的消息条数。

    归档清理**无条件执行**：老会话可能早已不在主存（被 100 条上限截断），
    此时 removed==0，但归档文件仍在 —— 不删就会出现"列表里删掉了、
    文件夹里还留着"的不一致（用户会当成没删掉）。
    """
    cid = _normalize_conversation_id(conversation_id)
    with _chat_manager._history_lock:
        before = len(_chat_manager.history)
        _chat_manager.history[:] = [
            m for m in _chat_manager.history
            if getattr(m, 'conversation_id', 'legacy') != cid
        ]
        removed = before - len(_chat_manager.history)
        if removed:
            # 同 clear_chat_history：破坏性操作绕过节流，避免删了又"复活"
            # （merge_disk=False 同样是防"复活"的一环）
            _chat_manager.flush_history(merge_disk=False)
    # 删的若是当前会话：先切回 legacy 再清理，避免后续消息写进已删会话；
    # 顺序不能反 —— activate 会把 previous 的 context 存回字典，之后再 pop 才干净。
    if getattr(_chat_manager, 'active_conversation_id', 'legacy') == cid:
        activate_chat_conversation('legacy')
    _chat_manager._conversation_contexts.pop(cid, None)
    try:
        from conversation_memory import get_memory
        get_memory().forget_conversation(cid)
    except Exception as e:
        logger.debug('清理会话记忆桶失败（忽略）: %s', e)
    try:
        import chat_archive as _ca
        _ca.purge(conversation_id=cid)
    except (Exception, SystemExit) as e:
        logger.debug('聊天归档清理失败（忽略）: %s', e)
    return removed


def delete_chat_message(content: str, timestamp: str = None, role: str = None) -> int:
    """删除单条已持久化的对话记录（按内容精确匹配，可选时间&角色限定）。

    返回实际删除条数。与 clear_chat_history 共用 _history_lock 保证并发安全。
    """
    if content is None:
        return 0
    removed = 0
    with _chat_manager._history_lock:
        kept = []
        for m in _chat_manager.history:
            same_content = (m.content == content)
            same_ts = (timestamp is None) or (m.timestamp == timestamp)
            same_role = (role is None) or (m.role == role)
            if same_content and same_ts and same_role:
                removed += 1
            else:
                kept.append(m)
        if removed:
            _chat_manager.history[:] = kept
            # 同 clear_chat_history：删除单条也绕过节流，保证内存与磁盘一致
            # （merge_disk=False 防的正是"删掉的又被合并回来"）
            _chat_manager.flush_history(merge_disk=False)
            try:
                import chat_archive as _ca
                _ca.purge(message={'role': role, 'content': content, 'time': timestamp})
            except (Exception, SystemExit) as e:
                logger.debug('聊天归档清理失败（忽略）: %s', e)
    return removed

def get_conclusions() -> List[Dict]:
    """获取分析结论（占位函数）"""
    return []
