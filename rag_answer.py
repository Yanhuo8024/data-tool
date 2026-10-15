"""「RAG 问答」的统一检索与生成出口。

## 为什么需要这个模块

原 `/api/rag/query` 只查内存态的 `rag_engine`，存在三个互相独立的缺陷：

1. **从不生成**：`rag_engine.query()` 的 `llm_function` 参数在生产代码零调用方，
   路由也只传 `top_k`，必然走 `else` 分支 —— `answer` 恒为
   `已检索到 N 条与「X」相关的片段。`，用户看不到任何实质回答。
2. **无持久化**：`rag_engine` 是纯内存单例，进程重启即清空；
   唯一写入方是 AI 助手的 `file_rag` 工具，因此它是 AI 助手的副产品。
3. **与知识库脱节**：知识库管理页上传的文档写入 SQLite `knowledge_docs`
   （143 篇），而本接口查的是 `rag_engine`（0 篇），且页面提示还引导用户
   去知识库页上传 —— 照做之后仍然是 0 条。

本模块把三条来源接通为一次回答：

    企业知识库(SQLite) + 内置知识点(JSON)  ->  复用 ai_assistant._joined_kb_hits
    用户文件(内存 rag_engine)              ->  复用 rag_engine.query
    综合生成                                ->  复用 ai_assistant._rag_answer

检索与生成**一律复用 ai_assistant 的既有实现**，不在此处二次实现，
以免形成双路径漂移（该仓库的既有设计原则）。

## 开关

`RAG_ANSWER_LLM=0` 关闭 LLM 生成，只返回检索摘要（用于离线排查 / 无凭据环境）。
"""

import logging
import os
from typing import Dict, List

logger = logging.getLogger(__name__)


def _llm_enabled() -> bool:
    """是否启用 LLM 综合生成；env `RAG_ANSWER_LLM=0` 关闭。"""
    return os.environ.get('RAG_ANSWER_LLM', '1').strip().lower() not in ('0', 'false', 'no', 'off')


def _knowledge_hits(question: str) -> List[Dict]:
    """检索企业知识库(SQLite) + 内置知识点(JSON)，复用 AI 助手的合并逻辑。

    注意：`_joined_kb_hits` 返回的 `merged` 始终包含两边的全部命中，
    其 `is_kb` / `ent_is_kb` 标志只决定调用方是否"抢答"，与内容无关 ——
    本接口是用户主动提问，故不受该抑制影响，直接取 `merged`。
    """
    try:
        from ai_assistant import _joined_kb_hits
        merged, _is_kb, _ent_is_kb = _joined_kb_hits(question, question, {})
        return list(merged or [])
    except Exception as e:
        # 无 Flask app context（脚本/单测）时属预期，降级为空并留下调试线索
        logger.debug("知识库检索不可用（已跳过）: %s", e)
        return []


def _file_hits(question: str, top_k: int) -> Dict:
    """检索用户上传文件（内存态 rag_engine）。返回其原生 {answer, sources, chunks}。"""
    try:
        from rag_unified import rag_engine
        return rag_engine.query(question, top_k=top_k) or {}
    except Exception as e:
        logger.debug("用户文件 RAG 不可用（已跳过）: %s", e)
        return {}


def _merge_sources(kb_entries: List[Dict], file_res: Dict) -> List[Dict]:
    """把知识库条目与文件片段统一成前端既有可渲染的 sources 结构。

    前端读取 `doc_name` 与 `score` 两个字段，故保持该结构不变；
    来源类别通过 `doc_name` 前缀与 `kind` 字段同时标注（前端零改动即可区分）。
    知识库条目的原始分数是关键词/TF-IDF 量纲（10~100+），
    直接展示会变成"相似度:120"，故改为按命中排名衰减的相关度（0.35~1.0）。
    """
    out: List[Dict] = []
    for i, e in enumerate(kb_entries or []):
        name = str(e.get('name') or '未命名').strip()
        text = str(e.get('detail') or e.get('desc') or '').strip()
        out.append({
            'doc_name': f'[知识库] {name}',
            'score': round(max(0.35, 1.0 - i * 0.12), 3),
            'snippet': text[:150],
            'kind': 'knowledge',
        })
    for s in (file_res.get('sources') or []):
        item = dict(s)
        item['doc_name'] = f"[用户文件] {item.get('doc_name') or ''}".strip()
        item['kind'] = 'file'
        out.append(item)
    return out


def _as_hits(kb_entries: List[Dict], file_res: Dict) -> List[Dict]:
    """把两类来源整理成 `_rag_answer` 需要的条目格式（含 name/detail/category）。"""
    hits: List[Dict] = []
    for e in (kb_entries or []):
        hits.append(dict(e))
    for c in (file_res.get('chunks') or [])[:3]:
        text = str(c.get('text') or '')
        hits.append({
            'name': f"用户文件 {c.get('doc') or ''}".strip(),
            'desc': text[:180],
            'detail': text,
            'tab': '用户文件',
            'category': 'user_file',
            'level': 'core',          # 用户自有数据视为权威，注入时优先
        })
    return hits


def _generate(question: str, hits: List[Dict]) -> str:
    """复用 AI 助手的综合生成；不可用 / 关闭时返回 None（由调用方走兜底）。"""
    if not hits or not _llm_enabled():
        return None
    try:
        from ai_assistant import _rag_answer
        return _rag_answer(question, hits, None, depth='balanced')
    except Exception as e:
        logger.warning("RAG 综合生成失败，回退检索摘要: %s", e)
        return None


def _fallback_answer(kb_entries: List[Dict], file_res: Dict) -> str:
    """模型不可用时的可读兜底：列出**真正命中的条目原文**，而不是一句计数废话。"""
    lines: List[str] = []
    if kb_entries:
        lines.append(f"检索到 {len(kb_entries)} 条相关知识：")
        for i, e in enumerate(kb_entries[:5], 1):
            name = str(e.get('name') or '未命名').strip()
            text = str(e.get('detail') or e.get('desc') or '').strip()
            lines.append(f"{i}. {name}：{text[:120]}")
    chunks = file_res.get('chunks') or []
    if chunks:
        lines.append(f"用户文件中检索到 {len(chunks)} 个相关片段：")
        for i, c in enumerate(chunks[:3], 1):
            text = str(c.get('text') or '').replace('\n', ' ')
            lines.append(f"{i}. [{c.get('doc') or ''}] {text[:120]}")
    if not lines:
        return ("未在你上传的文件与知识库中检索到相关内容。"
                "可先在「AI 助手」选中一个数据文件，或在「知识库管理」页补充文档后重试。")
    lines.append("")
    lines.append("（当前未生成综合回答，以上为检索到的原文，请直接参考。）")
    return '\n'.join(lines)


def answer_with_knowledge(question: str, top_k: int = 5, doc_name: str = None) -> Dict:
    """RAG 问答统一入口：融合检索 + LLM 综合生成。

    返回与 `/api/rag/query` 原契约兼容的 `{"answer", "sources", "chunks"}`，
    并额外提供 `knowledge_hits`（命中的知识条目名，便于前端或排查使用）。
    """
    q = (question or '').strip()
    if not q:
        return {'answer': '请先输入问题。', 'sources': [], 'chunks': [],
                'knowledge_hits': [], 'llm_synthesized': False}

    kb_entries = _knowledge_hits(q)
    file_res = _file_hits(q, top_k)
    # 文档问答（NLP 页）：只保留指定文档的片段，避免与「知识库 RAG」混用范围
    if doc_name and isinstance(file_res, dict):
        key = str(doc_name).strip().lower()
        srcs = []
        for s0 in (file_res.get('sources') or []):
            name = str(s0.get('doc_name') or '').lower()
            if key in name or name in key or key in s0.get('doc_name', ''):
                srcs.append(s0)
        if srcs:
            names = {s0.get('doc_name') for s0 in srcs}
            file_res = dict(file_res)
            file_res['sources'] = srcs
            file_res['chunks'] = [
                c for c in (file_res.get('chunks') or [])
                if (c.get('doc_name') if isinstance(c, dict) else None) in names
                or not isinstance(c, dict)
            ]
        else:
            file_res = dict(file_res)
            file_res['sources'] = []
            file_res['chunks'] = []
            file_res['answer'] = file_res.get('answer') or ''
    hits = _as_hits(kb_entries, file_res)

    answer = _generate(q, hits)
    synthesized = bool(answer)
    if not answer:
        answer = _fallback_answer(kb_entries, file_res)

    return {
        'answer': answer,
        'sources': _merge_sources(kb_entries, file_res),
        'chunks': file_res.get('chunks') or [],
        'knowledge_hits': [str(e.get('name') or '') for e in kb_entries[:5]],
        'llm_synthesized': synthesized,
        'doc_filter': (doc_name or ''),
    }
