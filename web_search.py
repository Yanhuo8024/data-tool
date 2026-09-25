#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_search.py - 网页搜索模块（无 API key，必应/百度/DuckDuckGo HTML 端点）

供 AI 助手/Agent 使用：搜索网页获取最新资料，返回标题/链接/摘要。
- 无需注册、无需 key，适合本地工具
- 对结果做截断与去重，避免注入过长内容
- 出境开关：ALLOW_WEB_SEARCH（默认开启）。关闭后本模块所有入口直接返回提示，
  不发出任何外呼；策略与优先级见 egress_policy.py，清单见 design/DATA_EGRESS.md。
"""
import re
import html
import time
import logging
from urllib.parse import quote, unquote, urlparse, parse_qs

logger = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# 搜索结果缓存（相同问题 5 分钟内不重复搜索）
_cache = {}
_CACHE_MAX = 100
_CACHE_TTL = 300

# 提问词/无意义词（query 清洗时去除）
_STOP_WORDS = [
    '请问', '什么是', '是什么', '啥是', '何为', '介绍一下', '介绍下', '介绍',
    '解释一下', '解释下', '解释', '讲讲', '讲下', '说明', '科普', '了解下',
    '如何', '怎么', '怎样', '为什么', '为何', '帮我', '我想', '请', '一下',
    '区别', '含义', '意思', '原理', 'tell me', 'what is', "what's",
    'explain', 'how to', 'what does', 'meaning', 'the', 'of', 'and',
]


def _cache_get(key):
    item = _cache.get(key)
    if item and time.time() - item[0] < _CACHE_TTL:
        return item[1]
    if item:
        _cache.pop(key, None)
    return None


def _cache_put(key, value):
    if len(_cache) >= _CACHE_MAX:
        for k in sorted(_cache, key=lambda k: _cache[k][0])[: _CACHE_MAX // 2]:
            _cache.pop(k, None)
    _cache[key] = (time.time(), value)


def build_search_query(message: str, max_len: int = 60) -> str:
    """从用户问题提取搜索关键词：去提问词/标点/噪音，保留核心实体"""
    q = message.strip()
    lower = q.lower()
    for w in _STOP_WORDS:
        q = q.replace(w, ' ')
    q = re.sub(r'[，。？！、；：“”"\'()（）\[\]【】,.!?;:()\-—\s]+', ' ', q).strip()
    if len(q) < 2 and message.strip():
        q = message.strip()
    q = re.sub(r'\s+', ' ', q)[:max_len]
    return q


def _clean(text: str) -> str:
    """去除 HTML 标签并反转义实体"""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _decode_ddg_url(url: str) -> str:
    """DuckDuckGo 结果链接是重定向，解码出真实 URL"""
    if "duckduckgo.com/l/" in url or "uddg=" in url:
        m = re.search(r"[?&]uddg=([^&]+)", url)
        if m:
            return unquote(m.group(1))
    return url


def _bing_search(query: str, top_k: int = 5, timeout: int = 10) -> dict:
    """必应网页搜索（国内网络可达），解析 b_algo 结果块"""
    import requests
    url = "https://cn.bing.com/search?q=" + quote(query.strip()) + "&mkt=zh-CN"
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("bing 搜索失败: %s", e)
        return {"ok": False, "error": f"必应搜索失败: {e}"}

    text = resp.text
    results = []
    seen = set()
    blocks = re.findall(r'<li class="b_algo".*?</li>', text, re.S)
    for b in blocks[: top_k * 2]:
        hm = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not hm:
            continue
        title = _clean(hm.group(2))
        u = hm.group(1)
        if not title or not u or u in seen or "bing.com" in u:
            continue
        pm = re.search(r"<p[^>]*>(.*?)</p>", b, re.S)
        seen.add(u)
        results.append({
            "title": title[:200],
            "url": u[:300],
            "snippet": (_clean(pm.group(1)) if pm else "")[:300],
        })
        if len(results) >= top_k:
            break
    return {"ok": True, "results": results, "query": query}


def _baidu_search(query: str, top_k: int = 5, timeout: int = 10) -> dict:
    """百度网页搜索兜底"""
    import requests
    url = "https://www.baidu.com/s?wd=" + quote(query.strip())
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("baidu 搜索失败: %s", e)
        return {"ok": False, "error": f"百度搜索失败: {e}"}

    text = resp.text
    results = []
    seen = set()
    # 百度结果块：<h3 class="t"><a href="...">标题</a></h3> + <span class="content-right_...">摘要</span>
    blocks = re.findall(r'<h3[^>]*class="[^"]*t[^"]*"[^>]*>.*?</h3>', text, re.S)
    snips = re.findall(r'<span[^>]*class="[^"]*content-right[^"]*"[^>]*>(.*?)</span>', text, re.S)
    for i, b in enumerate(blocks[: top_k * 2]):
        am = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not am:
            continue
        title = _clean(am.group(2))
        u = am.group(1)
        if not title or not u or u in seen:
            continue
        seen.add(u)
        results.append({
            "title": title[:200],
            "url": u[:300],
            "snippet": (_clean(snips[i]) if i < len(snips) else "")[:300],
        })
        if len(results) >= top_k:
            break
    return {"ok": True, "results": results, "query": query}


def _ddg_search(query: str, top_k: int = 5, timeout: int = 10) -> dict:
    """DuckDuckGo HTML 搜索（备用源）"""
    import requests
    url = "https://html.duckduckgo.com/html/?q=" + quote(query.strip())
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("ddg 搜索失败: %s", e)
        return {"ok": False, "error": f"DuckDuckGo 搜索失败: {e}"}

    text = resp.text
    results = []
    seen = set()
    blocks = re.split(r'class="result results_links', text)[1:]
    if not blocks:
        titles = list(re.finditer(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', text))
        snips = [_clean(s) for s in re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', text)]
        for i, m in enumerate(titles):
            t = _clean(m.group(2))
            u = _decode_ddg_url(html.unescape(m.group(1)))
            if not t or not u or u in seen:
                continue
            seen.add(u)
            results.append({"title": t, "url": u, "snippet": snips[i] if i < len(snips) else ""})
            if len(results) >= top_k:
                break
    else:
        for b in blocks[: top_k * 2]:
            tm = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b)
            if not tm:
                continue
            title = _clean(tm.group(2))
            u = _decode_ddg_url(html.unescape(tm.group(1)))
            if not title or not u or u in seen:
                continue
            sm = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', b)
            seen.add(u)
            results.append({
                "title": title[:200],
                "url": u[:300],
                "snippet": (_clean(sm.group(1)) if sm else "")[:300],
            })
            if len(results) >= top_k:
                break
    return {"ok": True, "results": results, "query": query}


def web_search(query: str, top_k: int = 5, timeout: int = 10, use_cache: bool = True) -> dict:
    """
    搜索网页，返回 {ok, results: [{title, url, snippet}], error?}
    多源链：百度 → 必应(cn.bing) → DuckDuckGo，任一成功即返回。
    百度优先：中文查询相关性显著更好，且必应对无 Cookie 请求返回降级结果
    （实测搜"宁德时代"返回宁德市旅游），但降级页仍有 b_algo 块、会被误判为成功。
    自动清洗提问词；相同问题 5 分钟内命中缓存不重复搜索。
    出境开关 ALLOW_WEB_SEARCH=0 时不外呼，直接返回 {ok: False, disabled: True}。
    """
    if not query or not query.strip():
        return {"ok": False, "error": "查询为空"}

    try:
        from egress_policy import web_search_enabled, disabled_reason
        if not web_search_enabled():
            logger.info("联网搜索已关闭，跳过外呼: %s", query[:60])
            return {"ok": False, "disabled": True, "error": disabled_reason('web_search')}
    except Exception as e:  # 策略模块缺失不应让搜索整体不可用，但必须留下痕迹
        logger.warning("出境策略读取失败，按默认（开启）继续: %s", e)

    cleaned = build_search_query(query)
    search_term = cleaned if cleaned else query.strip()
    if use_cache:
        cached = _cache_get(search_term + f"|{top_k}")
        if cached is not None:
            return cached

    last_err = ""
    for fn in (_baidu_search, _bing_search, _ddg_search):
        try:
            r = fn(search_term, top_k=top_k, timeout=timeout)
            if r.get("ok") and r.get("results"):
                r['query'] = query
                if use_cache:
                    _cache_put(search_term + f"|{top_k}", r)
                return r
            last_err = r.get("error", "") or "无结果"
        except Exception as e:
            last_err = str(e)
            logger.warning("搜索源异常: %s", e)

    return {"ok": False, "error": last_err or "所有搜索源均失败"}


def format_results(query: str, top_k: int = 5) -> str:
    """把搜索结果格式化为紧凑文本（供 LLM 阅读）"""
    res = web_search(query, top_k=top_k)
    if not res.get("ok"):
        return res.get("error", "搜索失败")
    items = res.get("results", [])
    if not items:
        return "未找到相关网页结果。"
    lines = [f"针对「{query}」的搜索结果："]
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['title']}\n   链接: {it['url']}\n   摘要: {it['snippet']}")
    return "\n".join(lines)


def search_for_answer(query: str, top_k: int = 5) -> dict:
    """带结果的完整返回（供聊天路径使用）"""
    res = web_search(query, top_k=top_k)
    return {
        "ok": res.get("ok", False),
        "error": res.get("error", ""),
        "query": query,
        "results": res.get("results", []),
    }


if __name__ == "__main__":
    # 自测
    import json
    r = web_search("A股 上证指数 2026年8月 行情", top_k=3)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:800])
