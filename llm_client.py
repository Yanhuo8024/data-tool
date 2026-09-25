#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
llm_client.py - 大模型统一接口
支持多提供商：DeepSeek、OpenAI、Groq、Ollama、智谱、DashScope、自定义

统一走 OpenAI 兼容的 /v1/chat/completions 协议（Ollama 原生支持），
内置指数退避重试、动态超时、usage 透传与通用 SSE 流式。
"""

import os
import json
import logging
import threading
import time

import requests

logger = logging.getLogger(__name__)

# 可重试状态码（限流/临时服务端错误）
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 请求超时基数（秒）；随 max_tokens 线性增加
BASE_TIMEOUT = 30
TIMEOUT_PER_TOKEN = 0.02  # 每 token 增加 0.02s（4000 tokens → +80s）

# provider 默认配置
# 大模型厂商全家桶：方便用户在前端「⚙️ 模型设置」里自由切换
# 协议：除 anthropic 外都是 OpenAI 兼容 /chat/completions，anthropic 走原生 /v1/messages（见 _chat_anthropic）
_PROVIDER_URLS = {
    # ---- 国产 ----
    "sensenova": "https://token.sensenova.cn/v1/chat/completions",          # 商汤日日新（默认免费）
    "deepseek": "https://api.deepseek.com/v1/chat/completions",             # 深度求索
    "zhipu": "https://open.bigmodel.cn/api/paas/v4/chat/completions",        # 智谱 GLM
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",  # 阿里通义千问
    "baidu": "https://qianfan.baidubce.com/v2/chat/completions",            # 百度千帆（文心）
    "doubao": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",   # 字节豆包（火山方舟）
    "moonshot": "https://api.moonshot.cn/v1/chat/completions",               # 月之暗面 Kimi
    "yi": "https://api.lingyiwanwu.com/v1/chat/completions",                 # 零一万物
    "hunyuan": "https://api.hunyuan.tencent.com/v1/chat/completions",        # 腾讯混元
    "spark": "https://spark-api-open.xf-yun.com/v1/chat/completions",        # 讯飞星火（OpenAI 兼容中转）
    # ---- 海外 ----
    "openai": "https://api.openai.com/v1/chat/completions",
    "anthropic": "https://api.anthropic.com/v1/messages",                    # 原生 /messages（非 OpenAI 协议，走 _chat_anthropic）
    "google": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",  # Gemini（OpenAI 兼容）
    "xai": "https://api.x.ai/v1/chat/completions",                           # Elon Musk Grok
    "mistral": "https://api.mistral.ai/v1/chat/completions",                 # Mistral AI
    "azure": "",                                                            # Azure OpenAI（必须填 apiUrl）
    "groq": "https://api.groq.com/openai/v1/chat/completions",               # Groq（Llama 推理加速，免费层）
    "ollama": "http://localhost:11434/v1/chat/completions",                  # Ollama 本地
    # minimax 是 sensenova 的别名（保留兼容：早期 .env / localStorage 用 minimax 标识日日新）
    "minimax": "https://token.sensenova.cn/v1/chat/completions",
    "custom": "",                     # 用户自定义 OpenAI 兼容
}
_PROVIDER_DEFAULT_MODELS = {
    # 国产 ----
    # 2026-09-17 实测（新 sensenova key）：glm-5.2 / deepseek-v4-flash / deepseek-v4-pro /
    # sensenova-6.8-flash-lite 均 200；kimi-k3 持续 429（ModelAccountRpmRateLimitExceeded，
    # 间隔 75s 复测仍 429，判定为该账号配额不可用），故默认不再指向 kimi-k3。
    "sensenova": "glm-5.2",                          # 日日新默认（1M 上下文 / 131K 输出）
    "deepseek": "deepseek-v4-flash",
    # zhipu：glm-4.7-flash 已按用户要求从模型库移除，默认降为 glm-4-flash
    "zhipu": "glm-4-flash",
    "dashscope": "qwen-turbo",                       # 通义千问 Turbo（便宜）
    "baidu": "ernie-4.5-8k",                         # 文心一言 4.5
    "doubao": "doubao-pro-32k",                      # 豆包 Pro
    "moonshot": "moonshot-v1-8k",                    # Kimi v1
    "yi": "yi-large",                                # 零一万物 Yi-Large
    "hunyuan": "hunyuan-pro",                        # 腾讯混元 Pro
    "spark": "generalv3.5",                          # 讯飞星火 v3.5
    # 海外 ----
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-sonnet-latest",         # Claude 3.5 Sonnet 最新
    "google": "gemini-2.0-flash",                    # Gemini 2.0 Flash（免费层）
    "xai": "grok-2-latest",                            # Grok 2
    "mistral": "mistral-large-latest",
    "azure": "gpt-4o",                                 # 需配合 apiUrl
    "groq": "llama-3.1-70b-versatile",
    "ollama": "qwen2.5:7b",
    "minimax": "glm-5.2",             # alias of sensenova（与 sensenova 默认保持一致）
    "custom": "",
}
# 服务端环境变量名（前端不填 Key 时从 env / .secure 兜底）
_ENV_KEY_NAMES = {
    "sensenova": "SENSENOVA_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "zhipu": "ZHIPU_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "baidu": "BAIDU_API_KEY",
    "doubao": "DOUBAO_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "yi": "YI_API_KEY",
    "hunyuan": "HUNYUAN_API_KEY",
    "spark": "SPARK_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "google": "GOOGLE_API_KEY",
    "xai": "XAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "azure": "AZURE_OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",             # 此前遗漏：_fallback_provider 已认 GROQ_API_KEY，
                                        # 但 _ENV_KEY_NAMES 缺失会导致自动选择/凭据探测漏掉它
    "minimax": "SENSENOVA_API_KEY",     # alias — 早期 .env 用 MINIMAX_API_KEY 也兼容
}


def _resolve_api_key(provider: str, config: dict) -> str:
    """API Key 解析：服务端环境变量 > 加密凭据库（.secure/credentials.enc）> 前端传入（仅兜底）"""
    # minimax 兼容路径：MINIMAX_API_KEY 也作为 sensenova 的兜底（早期 .env）
    candidate_envs = []
    if provider == "minimax":
        candidate_envs.extend(["MINIMAX_API_KEY", "SENSENOVA_API_KEY"])
    else:
        env_name = _ENV_KEY_NAMES.get(provider)
        if env_name:
            candidate_envs.append(env_name)

    for env_name in candidate_envs:
        env_key = os.getenv(env_name, "")
        if env_key:
            return env_key

    try:
        from secure_config import get_llm_api_key
        stored = get_llm_api_key(provider)
        if stored:
            return stored
    except Exception:
        pass
    return config.get("apiKey", "")


def _resolve_url(provider: str, config: dict) -> str:
    """URL 解析：自定义 URL 优先；兼容旧的 /api/generate Ollama 地址自动升级；custom 缺尾自动补 /chat/completions"""
    url = config.get("apiUrl", "") or _PROVIDER_URLS.get(provider, "")
    if provider == "ollama" and url:
        # 旧配置指向 /api/generate 时自动升级到 OpenAI 兼容端点
        if "/api/generate" in url:
            base = url.split("/api/generate")[0]
            url = base + "/v1/chat/completions"
    elif provider == "custom" and url and "/chat/completions" not in url:
        # custom 模式用户经常只填 base，自动补 OpenAI 兼容路径
        url = url.rstrip("/") + "/chat/completions"
    return url


def _resolve_model(provider: str, config: dict) -> str:
    model = config.get("model", "")
    if not model:
        env_model = os.getenv(f"{provider.upper()}_MODEL", "")
        model = env_model or _PROVIDER_DEFAULT_MODELS.get(provider, "gpt-4o-mini")
    return model


def _compute_timeout(max_tokens: int = None) -> float:
    return BASE_TIMEOUT + (max_tokens or 0) * TIMEOUT_PER_TOKEN


class LLMConnectionError(RuntimeError):
    """LLM 服务连接失败（网络不可达），可尝试回退其他 provider"""


def _fallback_provider() -> str:
    """检测可用的免费云端 provider（用于本地 Ollama 不可用时的自动回退）"""
    for prov, env in (('sensenova', 'SENSENOVA_API_KEY'),
                      ('groq', 'GROQ_API_KEY'),
                      ('zhipu', 'ZHIPU_API_KEY')):
        if os.getenv(env):
            return prov
        try:
            from secure_config import get_llm_api_key
            if get_llm_api_key(prov):
                return prov
        except Exception:
            pass
    return None


# ---------------------------------------------------------------------------
# 自动选择大模型（多模型协作）——候选 provider 枚举
# ---------------------------------------------------------------------------
# 优先级：已验证可用的免费额度优先，其次常见云端；用于多 Agent 各阶段轮换分配。
# 2026-09-17：sensenova 重新拥有有效凭据（实测 4 个模型可用）→ 提到首位；
# 其默认模型已由 kimi-k3（该账号持续 429）调整为 glm-5.2。
_AUTO_PROVIDER_PRIORITY = (
    'sensenova', 'zhipu', 'groq', 'dashscope', 'deepseek', 'moonshot',
    'google', 'openai', 'anthropic', 'mistral', 'xai',
    'baidu', 'doubao', 'yi', 'hunyuan', 'spark',
)
# 自动选择时**永不**纳入的 provider：
# - ollama：本机服务，用户明确要求"禁止网站自动拉起 Ollama"
# - custom / azure：必须由用户提供自定义 apiUrl，无法凭内置配置自动使用
# - minimax：sensenova 的别名，避免同一服务被重复计入而浪费"轮换"名额
_AUTO_PROVIDER_EXCLUDED = {'ollama', 'custom', 'azure', 'minimax'}


def list_available_providers() -> list:
    """列出当前**确有可用凭据**的云端 provider（自动选择大模型用）。

    判定顺序：环境变量 > 加密凭据库（.secure/credentials.enc）；二者皆无则视为不可用。
    仅做凭据探测，**不发起任何网络请求**（零额度、零延迟），因此不会因探活而消耗配额。

    Returns:
        [{'provider': str, 'model': str, 'source': 'env'|'secure'}...]，按优先级降序；
        无任何可用 provider 时返回空列表。
    """
    available = []
    ordered = list(_AUTO_PROVIDER_PRIORITY) + [
        p for p in _PROVIDER_URLS if p not in _AUTO_PROVIDER_PRIORITY]
    seen = set()
    for prov in ordered:
        if prov in seen or prov in _AUTO_PROVIDER_EXCLUDED:
            continue
        seen.add(prov)
        source = None
        env_name = _ENV_KEY_NAMES.get(prov)
        if env_name and os.getenv(env_name):
            source = 'env'
        else:
            try:
                from secure_config import get_llm_api_key
                if get_llm_api_key(prov):
                    source = 'secure'
            except Exception:
                source = None
        if not source:
            continue
        model = os.getenv(f"{prov.upper()}_MODEL", "") or _PROVIDER_DEFAULT_MODELS.get(prov, "")
        if not model:
            continue
        available.append({'provider': prov, 'model': model, 'source': source})
    return available


# ---------------------------------------------------------------------------
# 云端 provider 请求节流（自适应）
# ---------------------------------------------------------------------------
# 为什么不用固定间隔：agent 多轮调用是"串行消费"的，固定间隔在**健康态**纯属空等。
# 实测（零额度探针，见 output/_probe_throttle 口径）：固定 8s 下，4 轮 agent
# （净请求 2.5s/轮）要空等 16.5s；6 轮约 30s —— 与模型快慢无关，是纯增延迟。
# 且当前线上主要错误是 1305「该模型当前访问量过大」(服务端拥塞)，**预先固定间隔无法
# 预防它**，属于"代价照付、收益落空"。
#
# 改为自适应：基础间隔保持轻量，仅在真的收到 429 时按 provider 指数抬升，
# 连续成功后再逐步衰减回基础值 —— 健康态零额外空等，限流态仍比固定值更贴真实窗口。
#
# 环境变量：
#   CLOUD_MIN_INTERVAL  健康态基础间隔（秒），默认 2.5；要恢复旧保守行为可设 8
#   CLOUD_MAX_INTERVAL  限流态间隔上限（秒），默认 30
#   CLOUD_DECAY_AFTER   连续成功多少次衰减一档，默认 4
_CLOUD_MIN_INTERVAL = float(os.environ.get("CLOUD_MIN_INTERVAL", "2.5"))
_CLOUD_MAX_INTERVAL = float(os.environ.get("CLOUD_MAX_INTERVAL", "30"))
_CLOUD_DECAY_AFTER = max(1, int(os.environ.get("CLOUD_DECAY_AFTER", "4")))
_LAST_CLOUD_CALL: dict = {}   # 节流桶 -> 上次调用开始时刻
_ADAPT_INTERVAL: dict = {}    # 节流桶 -> 当前生效间隔（仅被 429 抬升时存在）
_OK_STREAK: dict = {}         # 节流桶 -> 连续成功次数

# 节流粒度（2026-09-18）：默认按 (provider, model) 分桶。
# 依据：sensenova 的限流报错为 `ModelAccountRpmRateLimitExceeded`（按"模型×账号"计），
# 而多Agent管线的三个阶段用的是同一 provider 下的**三个不同模型**。旧的 provider 级
# 分桶会把三个并行阶段的调用起始强行串行化（每次 ≥2.5s），凭空给并行执行加回串行延迟。
# 分桶后同一模型仍保持原有间隔保护；不同模型之间只保留一个很小的错峰闸门，
# 防止同一毫秒的瞬时并发把账号打爆。CLOUD_THROTTLE_SCOPE=provider 可一键回旧行为。
_CLOUD_THROTTLE_SCOPE = os.environ.get("CLOUD_THROTTLE_SCOPE", "model").strip().lower()
_CLOUD_CROSS_MODEL_INTERVAL = float(os.environ.get("CLOUD_CROSS_MODEL_INTERVAL", "0.5"))


def _bucket_key(provider: str, model: str = None) -> str:
    """节流桶键：默认 (provider, model)；provider 级回退或缺少 model 时退化为 provider。"""
    if not provider:
        return ''
    if _CLOUD_THROTTLE_SCOPE != 'model' or not model:
        return provider
    return f"{provider}::{model}"


def _current_interval(bucket: str) -> float:
    """该节流桶当前生效的节流间隔（未被限流时 = 基础间隔）"""
    return _ADAPT_INTERVAL.get(bucket, _CLOUD_MIN_INTERVAL)


# 并发安全（2026-09-18）：多Agent管线改为三阶段并行后，多个线程会同时调用同一 provider。
# _LAST_CLOUD_CALL 的"读-判-写"必须原子化，否则 3 个线程同时通过节流闸门 → 瞬时并发请求打爆
# 免费账号 rpm（1302 账户限速）。per-bucket 锁保证同一桶的调用起始时刻间隔 ≥ 生效间隔；
# 不同桶互不阻塞。睡在锁内是有意的：排队线程在闸门处自然错峰。
_THROTTLE_GUARD = threading.Lock()
_THROTTLE_LOCKS: dict = {}   # bucket -> threading.Lock
_PROVIDER_GATE_LOCKS: dict = {}   # provider -> 跨模型错峰闸门锁
_PROVIDER_LAST_START: dict = {}   # provider -> 该 provider 任意模型的最近起始时刻（跨模型错峰）


def _throttle_cloud(provider: str, model: str = None):
    """云端 provider 调用前节流；ollama/custom 本地不节流。

    分桶（默认 per-model）后：同桶严格按生效间隔错峰；同一 provider 的不同模型之间
    仅保证 _CLOUD_CROSS_MODEL_INTERVAL 的最小起始间隔（避免瞬时并发，但不串行化）。
    """
    if provider in ('ollama', 'custom'):
        return
    bucket = _bucket_key(provider, model)
    with _THROTTLE_GUARD:
        lock = _THROTTLE_LOCKS.get(bucket)
        if lock is None:
            lock = _THROTTLE_LOCKS[bucket] = threading.Lock()
        _gate = None
        if _CLOUD_THROTTLE_SCOPE == 'model' and model:
            _gate = _PROVIDER_GATE_LOCKS.get(provider)
            if _gate is None:
                _gate = _PROVIDER_GATE_LOCKS[provider] = threading.Lock()
    if _gate is not None:
        # 跨模型闸门：同一 provider 的任意两次调用起始至少间隔 _CLOUD_CROSS_MODEL_INTERVAL。
        with _gate:
            _now = time.time()
            _last_any = _PROVIDER_LAST_START.get(provider, 0.0)
            _w = _CLOUD_CROSS_MODEL_INTERVAL - (_now - _last_any)
            if _last_any > 0 and _w > 0:
                time.sleep(_w)
            _PROVIDER_LAST_START[provider] = time.time()
    with lock:
        now = time.time()
        last = _LAST_CLOUD_CALL.get(bucket, 0.0)
        # 首次调用（last==0）视为无历史，立即放行，避免"进程内首调也要空等"。
        if last > 0:
            iv = _current_interval(bucket)
            wait = iv - (now - last)
            if wait > 0:
                logger.info("云端 %s 节流等待 %.1fs（当前间隔 %.1fs）", bucket, wait, iv)
                time.sleep(wait)
        _LAST_CLOUD_CALL[bucket] = time.time()


def note_rate_limited(provider: str, retry_after=None, model: str = None) -> float:
    """观测到 429 后抬升该节流桶的间隔（指数上升，封顶上限）。返回新间隔。

    抬升幅度取 max(当前×2, Retry-After) —— 使服务端明确要求的等待被尊重；
    抬升后清零连续成功计数，避免刚被限流就立刻衰减回去。
    """
    if not provider:
        return _CLOUD_MIN_INTERVAL
    bucket = _bucket_key(provider, model)
    cur = _current_interval(bucket)
    nxt = min(cur * 2.0, _CLOUD_MAX_INTERVAL)
    if retry_after:
        try:
            nxt = max(nxt, min(float(retry_after), _CLOUD_MAX_INTERVAL))
        except (TypeError, ValueError):
            pass
    nxt = max(nxt, _CLOUD_MIN_INTERVAL)
    _ADAPT_INTERVAL[bucket] = nxt
    _OK_STREAK[bucket] = 0
    return nxt


def note_success(provider: str, model: str = None) -> float:
    """一次成功调用后衰减：连续成功达阈值则间隔减半，直至回归基础间隔。"""
    if not provider:
        return _CLOUD_MIN_INTERVAL
    bucket = _bucket_key(provider, model)
    s = _OK_STREAK.get(bucket, 0) + 1
    if s < _CLOUD_DECAY_AFTER:
        _OK_STREAK[bucket] = s
        return _current_interval(bucket)
    _OK_STREAK[bucket] = 0
    cur = _ADAPT_INTERVAL.get(bucket)
    if cur is None:
        return _CLOUD_MIN_INTERVAL
    nxt = cur / 2.0
    if nxt <= _CLOUD_MIN_INTERVAL:
        _ADAPT_INTERVAL.pop(bucket, None)
        return _CLOUD_MIN_INTERVAL
    _ADAPT_INTERVAL[bucket] = nxt
    return nxt


def _retry_after_seconds(resp) -> float:
    """解析 Retry-After 响应头（秒，可为小数）；不可解析返回 None。

    原实现用 `str(ra).isdigit()`，遇 `"1.5"` / 带空格 / HTTP-date 一律回退指数退避，
    会丢掉服务端明确给出的等待时长。这里统一按 float 解析并做合法性过滤。
    """
    try:
        ra = (resp.headers or {}).get('Retry-After')
    except Exception:
        return None
    if ra is None:
        return None
    try:
        v = float(str(ra).strip())
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def _post_with_retry(url: str, headers: dict, payload: dict,
                     timeout: float, max_retries: int = 3, provider: str = None,
                     model: str = None):
    """带指数退避重试的 POST：仅对限流/5xx/超时/连接错误重试（429 优先按 Retry-After 等待）

    provider/model 传入时会把 429/成功事件回报给自适应节流（note_rate_limited / note_success），
    使节流档位随真实限流情况收敛，而非依赖预先猜定的固定间隔。
    model 用于把节流粒度细化到 (provider, model) —— 多Agent管线三阶段同 provider 不同模型。
    """
    last_error = None
    escalated = False   # 同一逻辑调用只抬升一档，避免 3 次重试把间隔连翻 3 档
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 429 and provider and not escalated:
                escalated = True
                new_iv = note_rate_limited(provider, _retry_after_seconds(resp), model=model)
                logger.info("检测到 429 限流，%s 节流间隔抬升至 %.1fs", provider, new_iv)
            if resp.status_code in RETRYABLE_STATUS and attempt < max_retries:
                # 429 限流：优先读 Retry-After（免费额度 rpm 窗口约 20-30s，放宽到 30s）
                if resp.status_code == 429:
                    ra = _retry_after_seconds(resp)
                    wait = min(ra, 30) if ra is not None else min((2 ** attempt) * 0.8, 8)
                else:
                    wait = min((2 ** attempt) * 0.5, 6)
                logger.warning("LLM %s 返回 %s，%0.1fs 后重试 (%d/%d)",
                               url, resp.status_code, wait, attempt + 1, max_retries)
                time.sleep(wait)
                continue
            # 统一强制 UTF-8：部分云 API 响应头缺 charset，requests 默认 ISO-8859-1 会致中文乱码
            resp.encoding = "utf-8"
            if resp.status_code == 200 and provider:
                note_success(provider, model=model)
            return resp
        except requests.exceptions.RequestException as e:
            # 覆盖 Timeout / ConnectionError / 其他 requests 网络异常
            last_error = getattr(e, 'reason', None) or type(e).__name__
            if attempt < max_retries:
                time.sleep(1)
                continue
            # 连接类错误标记为可回退（本地 Ollama 未启动等场景）
            if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
                raise LLMConnectionError(f"连接失败（{last_error}），已重试 {max_retries} 次: {url}") from e
    raise RuntimeError(f"LLM 调用失败（{last_error or '未知错误'}），已重试 {max_retries} 次: {url}")


def _build_payload(provider: str, config: dict, messages: list,
                   tools: list = None, tool_choice: str = "auto",
                   max_tokens: int = None) -> dict:
    """构造 OpenAI 兼容请求体"""
    model = _resolve_model(provider, config)
    # 部分模型固定水温约束：kimi-k3 仅接受 temperature=1（传 0.3 会 400）
    temperature = float(config.get("temperature", 0.3))
    if model == "kimi-k3":
        temperature = 1.0
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens
    # 思考开关透传（2026-09-18）：sensenova reasoning 系实测支持 reasoning_effort:"none"
    # 关闭思考（0.7s vs 3.5s，且思考不再挤占 max_tokens 预算导致终稿截断）。
    # 仅在调用方显式设置时才发，避免对不支持该字段的 provider 造 400。
    _re_ = str((config or {}).get("reasoning_effort") or '').strip()
    if _re_:
        payload["reasoning_effort"] = _re_
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
    return payload


# ==================== Anthropic Claude 适配层 ====================
# Anthropic /v1/messages 协议与 OpenAI 不同：
#   - Header: x-api-key + anthropic-version（不是 Authorization: Bearer）
#   - system 是顶层字段，不在 messages 里
#   - tools 格式：{"name","description","input_schema"}（无 type/function 包装）
#   - 返回 content 是 blocks 数组（text / tool_use），不是 message.content 字符串
#   - tool_calls 由 tool_use blocks 重建为 OpenAI 格式返回
def _openai_tools_to_anthropic(tools: list) -> list:
    """[{type:function, function:{name,description,parameters}}] →
    [{name,description,input_schema}]"""
    out = []
    for t in tools or []:
        fn = t.get('function') if t.get('type') == 'function' else t
        if not fn:
            continue
        out.append({
            'name': fn.get('name', ''),
            'description': fn.get('description', ''),
            'input_schema': fn.get('parameters', {'type': 'object', 'properties': {}}),
        })
    return out


def _openai_messages_to_anthropic(messages: list) -> tuple:
    """拆出顶层 system，其余 messages 仅保留 user/assistant role。
    返回 (system_text, anthropic_messages)"""
    system_parts = []
    anthropic_msgs = []
    for m in messages or []:
        role = m.get('role')
        content = m.get('content')
        if role == 'system':
            if isinstance(content, str):
                system_parts.append(content)
            elif isinstance(content, list):
                # 列表里可能有 {'type':'text','text':'...'}
                for c in content:
                    if isinstance(c, dict) and c.get('type') == 'text':
                        system_parts.append(c.get('text', ''))
        elif role in ('user', 'assistant'):
            anthropic_msgs.append({'role': role, 'content': content or ''})
    # 合并 system
    system_text = '\n\n'.join(system_parts) if system_parts else None
    return system_text, anthropic_msgs


def _anthropic_response_to_openai(data: dict) -> dict:
    """Anthropic 响应 → OpenAI 兼容格式 {content, tool_calls?, usage}"""
    content_parts = []
    tool_calls = []
    for block in (data.get('content') or []):
        btype = block.get('type')
        if btype == 'text':
            content_parts.append(block.get('text', ''))
        elif btype == 'tool_use':
            # 转 OpenAI tool_calls 格式
            tool_calls.append({
                'id': block.get('id', ''),
                'type': 'function',
                'function': {
                    'name': block.get('name', ''),
                    'arguments': json.dumps(block.get('input') or {}, ensure_ascii=False),
                }
            })
    result = {'content': ''.join(content_parts)}
    if tool_calls:
        result['tool_calls'] = tool_calls
    if data.get('usage'):
        # Anthropic: input_tokens / output_tokens → OpenAI: prompt_tokens / completion_tokens
        u = data['usage']
        result['usage'] = {
            'prompt_tokens': u.get('input_tokens', 0),
            'completion_tokens': u.get('output_tokens', 0),
            'total_tokens': u.get('input_tokens', 0) + u.get('output_tokens', 0),
        }
    return result


def _chat_anthropic(provider: str, config: dict, messages: list,
                    tools: list = None, tool_choice: str = "auto",
                    max_tokens: int = None) -> dict:
    """调用 Anthropic /v1/messages，把请求/响应适配为 OpenAI 兼容格式返回"""
    api_key = _resolve_api_key(provider, config)
    url = _resolve_url(provider, config) or _PROVIDER_URLS.get(provider, "")
    if not url:
        raise RuntimeError("Anthropic 需要配置 apiUrl 或安装 ANTHROPIC_API_KEY")

    system_text, anth_messages = _openai_messages_to_anthropic(messages)
    payload = {
        "model": _resolve_model(provider, config),
        "messages": anth_messages,
        "max_tokens": int(max_tokens) if max_tokens else 4096,
        "temperature": float(config.get("temperature", 0.3)),
    }
    if system_text:
        payload["system"] = system_text
    anth_tools = _openai_tools_to_anthropic(tools)
    if anth_tools:
        payload["tools"] = anth_tools
        # Anthropic 不接 tool_choice='auto' 字符串；让它默认 auto 即可（不传）

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    _throttle_cloud(provider, payload.get("model"))
    resp = _post_with_retry(url, headers, payload, _compute_timeout(max_tokens),
                            provider=provider, model=payload.get("model"))
    resp.encoding = "utf-8"

    if resp.status_code != 200:
        logger.error("Anthropic API error %s: %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"Anthropic API error {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    return _anthropic_response_to_openai(data)


# ---------------- 思考兜底防泄漏（2026-09-19） ----------------
# reasoning 系模型（glm-5.2 等）把思考放 reasoning_content，正文才在 content。
# 当 max_tokens 被思考吃满时 content 为空，旧兜底会把 reasoning 当正文直出。
# 但**思考的第一步常常是"念题"**（实测 glm-5.2 返回 1343 字符的
# 「1. 分析请求：* 用户问题：…* 角色设定：…* 回答风格：…」）——
# 直出等于把系统提示词结构泄漏给用户（用户可见症状："AI 把我给它的提示词复述了一遍"）。
# 因此兜底前先识别"任务复述型"思考：命中则不采用，改为关掉思考重试一次直答。
_ECHO_MARKERS = ('分析请求', '角色设定', '用户问题：', '任务要求', '输出要求',
                 '回答风格', '风格：', '字数要求', '约束条件', '任务：', '身份：')


def looks_like_task_echo(text: str) -> bool:
    """思考/回答开头是否在"念题"（复述任务设定）而非作答。"""
    head = str(text or '')[:400]
    return sum(1 for m in _ECHO_MARKERS if m in head) >= 2


def _plain_answer_retry(provider: str, config: dict, messages: list, max_tokens):
    """复述兜底失败后的补救：关掉思考（reasoning_effort='none'）再要一次正文。
    仍拿不到干净正文则返回空串 —— 宁可返回空（上层会提示"未能生成"），也不泄漏提示词结构。"""
    cfg = dict(config or {}, reasoning_effort='none')
    try:
        r = _chat_with_provider(provider, cfg, messages, None, 'auto', max_tokens)
    except Exception as e:
        logger.warning('复述兜底重试失败: %s', e)
        return ''
    txt = str(r.get('content') or '')
    if txt.strip() and not looks_like_task_echo(txt):
        return txt
    return ''


def chat(messages: list, config: dict = None, tools: list = None,
         tool_choice: str = "auto", max_tokens: int = None) -> dict:
    """
    统一 LLM 调用接口（OpenAI 兼容协议，含 Ollama）

    config: {provider, apiKey, apiUrl, model, temperature}
    tools: OpenAI 格式的工具定义列表
    返回: {content: str, tool_calls: list?, usage: dict?}
    """
    config = config or {}
    # 默认 provider 为 sensenova（日日新，免费公测；与前端 core.js 默认一致）；
    # zhipu 等其它 provider 需显式传入或配置对应 *_API_KEY
    provider = config.get("provider", "sensenova")

    try:
        if provider == "anthropic":
            return _chat_anthropic(provider, config, messages, tools, tool_choice, max_tokens)
        return _chat_with_provider(provider, config, messages, tools, tool_choice, max_tokens)
    except LLMConnectionError as e:
        # 本地 provider（如 Ollama）连不上时，自动回退到环境变量可用的云端 provider
        fb = _fallback_provider()
        if fb and fb != provider:
            logger.warning("LLM provider %s 连接失败，自动回退到 %s: %s", provider, fb, e)
            fb_config = dict(config, provider=fb, apiUrl="", apiKey="", model=config.get("model") or "")
            result = _chat_with_provider(fb, fb_config, messages, tools, tool_choice, max_tokens)
            result["used_provider"] = fb
            result["fallback"] = True
            return result
        raise


def _chat_with_provider(provider: str, config: dict, messages: list,
                        tools: list = None, tool_choice: str = "auto",
                        max_tokens: int = None) -> dict:
    """对指定 provider 执行一次 chat 调用"""
    url = _resolve_url(provider, config)
    api_key = _resolve_api_key(provider, config)
    payload = _build_payload(provider, config, messages, tools, tool_choice, max_tokens)

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    _throttle_cloud(provider, payload.get("model"))  # 云端限流保护（agent 多轮调用场景）
    resp = _post_with_retry(url, headers, payload, _compute_timeout(max_tokens),
                            provider=provider, model=payload.get("model"))

    # 强制 UTF-8 解码：部分云 API 响应头不带 charset，requests 会回退 ISO-8859-1 导致中文乱码
    resp.encoding = "utf-8"

    if resp.status_code != 200:
        logger.error("API error %s: %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"API error {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"LLM 响应格式异常: {str(e)}")

    result = {"content": message.get("content", "")}
    # finish_reason 透传（2026-09-18）：'length' = 输出被 max_tokens 截断。
    # 此前不透出，上层终稿无法感知"被截断"，静默产出半截报告。
    try:
        _fr = data["choices"][0].get("finish_reason")
        if _fr:
            result["finish_reason"] = _fr
    except (KeyError, IndexError, TypeError, AttributeError):
        pass
    # P0 防空回复（2026-09-17）：reasoning 系模型（glm-5.2 / deepseek-v4-* / 6.8-flash-lite）
    # 把思考过程放在 reasoning_content（或 reasoning），最终答案才在 content。
    # 当 max_tokens 被推理过程吃满时 content 会是空串 —— 上层各调用点都只读 content，
    # 结果表现为「AI 不回复」。此处统一兜底：仅在**没有 tool_calls**（有工具调用说明
    # 空 content 是正常语义）且 content 为空时，回退使用 reasoning 文本。
    _reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    if (not str(result["content"] or "").strip()
            and not message.get("tool_calls")
            and str(_reasoning).strip()):
        if looks_like_task_echo(_reasoning):
            # 思考在"念题"：直出会把提示词结构泄漏给用户（见 _ECHO_MARKERS 注释）
            logger.warning("非流式响应无 content 且 reasoning 疑似任务复述（%d 字符），改为重试直答",
                           len(_reasoning))
            _plain = _plain_answer_retry(provider, config, messages, max_tokens)
            if _plain:
                result["content"] = _plain
                result["reasoning_retry"] = True
            else:
                result["reasoning_echo_blocked"] = True
        else:
            result["content"] = _reasoning
            result["reasoning_fallback"] = True
    if message.get("tool_calls"):
        result["tool_calls"] = message["tool_calls"]
    # usage 透传（供 token 配额/成本统计）；P2-4：统一归一为 prompt/completion/total
    if data.get("usage"):
        result["usage"] = _normalize_usage(data["usage"])
    return result


def _normalize_usage(u) -> dict:
    """usage 归一（P2-4）：把 provider 五花八门的计数键统一为
    prompt_tokens / completion_tokens / total_tokens。

    - 兼容 input_tokens / output_tokens（Anthropic 系）、prompt / completion 等别名；
    - 缺失 total 时按 prompt+completion 补齐（便于成本归因）；
    - 嵌套对象（如 prompt_tokens_details）与非数值一律跳过，绝不让归一本身抛错。
    """
    if not isinstance(u, dict):
        return {}
    out: dict = {}
    for k, v in u.items():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out[k] = int(v)
        elif isinstance(v, str):
            s = v.strip()
            if s.lstrip('+-').isdigit():
                out[k] = int(s)
    p = out.get('prompt_tokens', out.get('input_tokens'))
    c = out.get('completion_tokens', out.get('output_tokens'))
    if p is None:
        p = out.get('prompt')
    if c is None:
        c = out.get('completion')
    if p is not None:
        out['prompt_tokens'] = p
    if c is not None:
        out['completion_tokens'] = c
    if 'total_tokens' not in out and (p is not None or c is not None):
        out['total_tokens'] = int(p or 0) + int(c or 0)
    return out


def chat_stream(messages: list, config: dict = None, max_tokens: int = None):
    """
    通用 SSE 流式 LLM 调用（所有 OpenAI 兼容 provider，含 Ollama）：
    yield 增量文本 token。连接失败时自动回退可用云端 provider。
    用法：for token in chat_stream(messages, config): ...
    """
    config = config or {}
    provider = config.get("provider", "sensenova")
    try:
        yield from _stream_with_provider(provider, config, messages, max_tokens)
    except LLMConnectionError as e:
        fb = _fallback_provider()
        if fb and fb != provider:
            logger.warning("LLM provider %s 连接失败，流式回退到 %s: %s", provider, fb, e)
            fb_config = dict(config, provider=fb, apiUrl="", apiKey="", model=config.get("model") or "")
            yield from _stream_with_provider(fb, fb_config, messages, max_tokens)
        else:
            raise


def _stream_with_provider(provider: str, config: dict, messages: list,
                          max_tokens: int = None):
    """对指定 provider 执行一次流式调用"""
    url = _resolve_url(provider, config)
    api_key = _resolve_api_key(provider, config)
    payload = _build_payload(provider, config, messages, max_tokens=max_tokens)
    payload["stream"] = True

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # 流式路径不做起始节流（首 token 延迟敏感），但 429/成功仍按 (provider, model) 上报，
    # 使自适应档位能反映流式调用的真实限流情况。
    resp = _post_with_retry(url, headers, payload, _compute_timeout(max_tokens),
                            provider=provider, model=payload.get("model"))

    # 强制 UTF-8 解码：避免响应头缺 charset 时 requests 按 ISO-8859-1 解码导致中文乱码
    resp.encoding = "utf-8"

    if resp.status_code != 200:
        raise RuntimeError(f"API error {resp.status_code}: {resp.text[:200]}")

    _got_content = False
    _reasoning_buf = []
    for line in resp.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            break
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        try:
            delta = data["choices"][0]["delta"]
        except (KeyError, IndexError):
            continue
        token = delta.get("content", "")
        if token:
            _got_content = True
            yield token
            continue
        # reasoning 增量只缓存、不直出：思考过程不应混进正文。
        # 但若整个流结束都没有正文（推理占满 max_tokens），下面会用缓存兜底，
        # 否则前端会表现为「收到空回复」。
        _r = delta.get("reasoning_content") or delta.get("reasoning") or ""
        if _r:
            _reasoning_buf.append(_r)

    if not _got_content and _reasoning_buf:
        _text = "".join(_reasoning_buf)
        if looks_like_task_echo(_text):
            # 思考在"念题"：直出 = 把系统提示词结构泄漏给用户（实测 1343 字符的
            # 「分析请求 / 用户问题 / 角色设定 / 回答风格」原文）。改为关思考重试一次直答。
            logger.warning("流式响应无 content 且 reasoning 疑似任务复述（%d 字符），改为重试直答",
                           len(_text))
            _plain = _plain_answer_retry(provider, config, messages, max_tokens)
            if _plain:
                yield _plain
                return
            logger.warning("复述兜底重试仍无正文，返回空（不泄漏提示词结构）")
            return
        logger.warning("流式响应无 content，回退使用 reasoning（%d 字符）", len(_text))
        yield _text


def test_connection(config: dict) -> dict:
    """测试 LLM 连接"""
    try:
        result = chat([
            {"role": "user", "content": "Reply with: OK"}
        ], config, max_tokens=10)
        return {"ok": True, "model": config.get("model", "unknown"), "reply": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}
