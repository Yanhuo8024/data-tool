#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
agent_text_protocol.py - LLM 文本工具协议的解析与净化
=====================================================
自 ai_agent.py 拆出（2026-09-22，为满足行数门禁）。

职责边界（刻意保持零业务依赖，便于独立单测与复用）：
- 只做**纯文本处理**：从模型输出里解析工具调用、提取"思考过程"正文、剥离工具 JSON 残留；
- 输入 str、输出 str/list，不注册工具、不调用模型、不落盘、不读环境变量；
- **不 import ai_agent / core_config / 任何站点模块** → 无循环 import 风险。

ai_agent.py 仍 re-export 本模块的名字，既有的 `from ai_agent import parse_calls` 调用点不受影响。
"""
import re
import json
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

CALL_PATTERN_MARKDOWN = re.compile(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```')
CALL_PATTERN_BRACKET = re.compile(r'\[CALL:(\w+)\s+([^\]]+)\]')


def _extract_json_objects(text: str) -> List[str]:
    """用括号深度扫描提取文本中所有顶层 JSON 对象字符串。

    支持嵌套对象（如 {"params": {"file": "x", "chart_type": "scatter"}}），
    避免正则对嵌套括号过早截断导致 JSON 解析失败。
    """
    objects = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == '{':
            depth, j, in_str, esc = 0, i, False, False
            while j < n:
                ch = text[j]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == '\\':
                        esc = True
                    elif ch == '"':
                        in_str = False
                else:
                    if ch == '"':
                        in_str = True
                    elif ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            break
                j += 1
            if j < n and depth == 0:
                objects.append(text[i:j + 1])
                i = j + 1
                continue
        i += 1
    return objects


def parse_calls(text: str) -> List[Dict]:
    """Parse tool calls from LLM output (supports JSON and [CALL:] format)"""
    calls = []

    # JSON 格式优先（括号深度扫描，支持嵌套参数对象）
    for obj_str in _extract_json_objects(text):
        try:
            data = json.loads(obj_str)
            if 'tool' in data:
                calls.append({
                    'tool': data['tool'],
                    'params': data.get('params', {})
                })
        except json.JSONDecodeError as e:
            logger.debug("跳过非法 JSON 片段: %s", e)
            continue

    if calls:
        return calls

    # Try markdown JSON format
    for match in CALL_PATTERN_MARKDOWN.finditer(text):
        try:
            data = json.loads(match.group(1))
            if 'tool' in data:
                calls.append({
                    'tool': data['tool'],
                    'params': data.get('params', {})
                })
        except json.JSONDecodeError as e:
            logger.debug("跳过非法 markdown JSON: %s", e)
            continue

    if calls:
        return calls

    # Fallback to [CALL:tool key=value] format
    for match in CALL_PATTERN_BRACKET.finditer(text):
        tool_name = match.group(1)
        params_str = match.group(2)

        params = {}
        for param_match in re.finditer(r'(\w+)=([^\s\]]+)', params_str):
            key = param_match.group(1)
            value = param_match.group(2).strip('"\'')
            params[key] = value

        calls.append({'tool': tool_name, 'params': params})

    if calls:
        return calls

    # 宽容解析（不锚定行首/行尾）：模型输出的简写 "tool名/技能名 {扁平json}"，
    # 且可能与文字粘在同一行，如：
    #   company-analysis {"symbol": "002594.SZ"}请帮我分析宁德时代
    # 行锚定正则会漏掉这种粘连格式 → 整段被当成正文泄漏给用户
    for m in re.finditer(r'(?<![a-z0-9_-])([a-z][a-z0-9_-]{1,39})\s*(\{[^\{\}]*\})', text or ''):
        try:
            p = json.loads(m.group(2))
            if isinstance(p, dict):
                calls.append({'tool': m.group(1), 'params': p})
        except json.JSONDecodeError as e:
            logger.debug("跳过粘连简写片段: %s", e)
            continue

    if calls:
        return calls

    # 抢救截断输出：免费模型常先输出中文铺垫再写 JSON，写一半被 max_tokens 截断，
    # 导致整轮"看不到工具调用"。从残缺文本中提取工具名与键值对重建调用（尽力而为）。
    if not calls:
        m = re.search(r'[{\[]?\s*["\']tool["\']\s*:\s*["\']([a-z][a-z0-9_-]*)["\']', text or '', re.I)
        if m:
            tool = m.group(1)
            params = {}
            for pm in re.finditer(r'["\']([a-z_][a-z0-9_]*)["\']\s*:\s*["\']([^"\']*)["\']', text or '', re.I):
                k, v = pm.group(1), pm.group(2)
                if k != 'tool' and k not in params:
                    params[k] = v
            calls.append({'tool': tool, 'params': params})

    return calls


def _strip_tool_json(content: str) -> str:
    """移除文本中的工具调用 JSON 对象（含嵌套 params，按括号配平扫描）。

    旧正则 r'\\{\\s*"tool"[\\s\\S]*?\\}' 会在嵌套 JSON（params 内层 }）处提前截断，
    残留孤儿右括号；这里逐字符配平扫描，完整移除整个对象。"""
    out = []
    i, n = 0, len(content)
    while i < n:
        ch = content[i]
        if ch == '{':
            # 快速预判：接下来 40 字符内是否出现 "tool" 键（工具调用对象特征）
            if content.find('"tool"', i, min(i + 40, n)) != -1:
                depth, k = 0, i
                in_str = esc = False
                while k < n:
                    c = content[k]
                    if in_str:
                        if esc:
                            esc = False
                        elif c == '\\':
                            esc = True
                        elif c == '"':
                            in_str = False
                    else:
                        if c == '"':
                            in_str = True
                        elif c == '{':
                            depth += 1
                        elif c == '}':
                            depth -= 1
                            if depth == 0:
                                break
                    k += 1
                if depth == 0 and k < n:
                    i = k + 1
                    continue
        out.append(ch)
        i += 1
    return ''.join(out)


def _extract_reasoning(text: str) -> str:
    """从 LLM 输出中提取供前端「思考过程」面板展示的推理正文。

    复用配平扫描的 _strip_tool_json 移除工具调用 JSON，再剥离 markdown 代码块、
    [CALL:...] 括号调用与「skill名 {扁平json}」粘连形式。只留纯推理文字。
    """
    if not text:
        return ''
    t = str(text)
    t = re.sub(r'```(?:json)?[\s\S]*?```', '', t)                    # 代码块
    t = re.sub(r'\[CALL:[^\]]*\]', '', t)                           # [CALL:...]
    t = re.sub(r'(?<![a-z0-9_-])[a-z][a-z0-9_-]{1,39}\s*\{[^\{\}]*\}', '', t)  # skillname {json}
    t = _strip_tool_json(t)                                          # 配平移除含 "tool" 的 JSON 对象
    t = re.sub(r'\n{3,}', '\n\n', t).strip()
    return t.strip()


def _clean_final_output(content: str) -> str:
    """清理最终输出中的工具调用 JSON/标记"""
    if not content:
        return content

    # 移除 markdown 代码块中的工具调用 JSON
    content = re.sub(r'```(?:json)?\s*\{\s*"tool"[\s\S]*?\}\s*```', '', content)
    content = re.sub(r'\[CALL:\w+[^\]]*\]', '', content)

    # 移除纯 JSON 工具调用（含嵌套 params，配平扫描）
    content = _strip_tool_json(content)

    # 移除"工具/技能名 {扁平json}"式简写泄漏（含与文字粘连的变体，如
    # company-analysis {"symbol": "002594.SZ"}请帮我分析宁德时代）
    content = re.sub(r'(?<![a-z0-9_-])[a-z][a-z0-9_-]{1,39}\s*\{[^\{\}]*\}', '', content)

    # 清理多余空行
    content = re.sub(r'\n{3,}', '\n\n', content).strip()
    return content
