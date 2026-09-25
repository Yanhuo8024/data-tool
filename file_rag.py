#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_rag.py - 用户上传文件的持久化语义索引与检索（RAG 增强）

背景：此前 RAG 仅覆盖"预生成知识库"(knowledge_data.json)，用户上传的 csv/xlsx
只走数据分析管线，Agent 无法"语义检索文件里讲了什么"。本模块复用 rag_unified 的
全局引擎(rag_engine)，把上传文件内容分块索引到内存 RAG，提供给 Agent 主动检索。

覆盖两类资料：
- 表格类（csv / xlsx / xls / parquet）：走 load_dataframe，索引列结构与样本行；
- 文档类（pdf / docx / txt / md / json / html）：正文抽取后入索引
  ——对应"读研报、财报、合同、产品规则"这类非结构化资料（原实现直接返回空）。

出境开关（ALLOW_FILE_SAMPLE_EGRESS，默认开启，策略见 egress_policy.py）：
- 开启：索引文本含**原始内容**（表格为前 6000 字符样本行，文档为正文），
  内容会随提问发往云端模型（见 E2）；
- 关闭：切换为「仅本地统计」——表格只索引列名/类型/缺失/分布汇总量，
  文档只索引文件名/类型/体积/字符数等元信息，不含任何原文片段。
  开关切换后已索引内容会自动重建，避免关闭后仍能检索到旧内容。
"""

import logging

from egress_policy import file_sample_egress_enabled, disabled_reason

logger = logging.getLogger(__name__)

# 已索引的文件（file_name -> {'doc_id': str, 'mode': 'sample'|'stats'}），避免重复索引
_INDEXED = {}


def _mode() -> str:
    """当前索引模式：sample（含样本行）/ stats（仅本地统计）"""
    return 'sample' if file_sample_egress_enabled() else 'stats'


def _stats_only(df) -> str:
    """仅本地统计：列类型 / 缺失 / 数值分布汇总量——不含任何原始样本行"""
    out = ['（仅本地统计模式：样本行未随提问发送，以下均为汇总量）',
           '列类型: ' + ' | '.join(f'{c}:{df[c].dtype}' for c in df.columns[:40])]
    miss = [f'{c}:{int(n)}' for c, n in df.isna().sum().items() if n]
    if miss:
        out.append('缺失: ' + ', '.join(miss[:40]))
    num = df.select_dtypes('number')
    if not num.empty:
        desc = num.describe().T
        keep = [c for c in ('mean', 'std', 'min', '50%', 'max') if c in desc.columns]
        out.append('数值分布:\n' + desc[keep].round(4).to_string()[:3000])
    else:
        obj = df.select_dtypes(exclude='number')
        if not obj.empty:
            out.append('分类列取值数: ' + ', '.join(
                f'{c}:{obj[c].nunique()}' for c in obj.columns[:30]))
    return '\n'.join(out)


# ==================== 文档类资料（非表格）====================
# 对应券商 Agent 的"读研报 / 财报 / 合同 / 产品规则"场景：这类资料不是表格，
# 走不了 load_dataframe，此前被静默跳过（Agent 看不到文件讲了什么）。
# 抽取走"能用的解析库就用、没有就明确说没有"，绝不因缺依赖而崩溃或谎报已读。
_DOC_EXTS = ('.pdf', '.docx', '.txt', '.md', '.markdown', '.log', '.json', '.html', '.htm')
_MAX_DOC_CHARS = 20000   # 单文档入索引上限（分块由 rag_unified 负责）


def _read_text_file(fp):
    """纯文本/结构化文本直读；编码兜底链与工程习惯一致"""
    raw = fp.read_bytes()
    for enc in ('utf-8-sig', 'utf-8', 'gbk', 'utf-16'):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError) as e:
            logger.debug('按 %s 解码 %s 失败，继续尝试下一编码: %s', enc, fp.name, e)
    return raw.decode('utf-8', errors='replace')


def _extract_pdf(fp):
    """PDF 正文抽取。返回 (text, pages)。三种库依次尝试，都缺则返回 (None, 0)。"""
    try:
        import fitz                                   # PyMuPDF，质量最好
    except ImportError:
        logger.debug('PyMuPDF 不可用，改用 pypdf / PyPDF2')
    else:
        try:
            doc = fitz.open(str(fp))
            pages = len(doc)
            text = '\n\n'.join(doc[i].get_text() for i in range(pages))
            doc.close()
            return text, pages
        except Exception as e:                        # 文件损坏/加密
            logger.warning('PyMuPDF 解析 %s 失败: %s', fp.name, e)
            return None, 0
    for mod in ('pypdf', 'PyPDF2'):
        try:
            reader = __import__(mod).PdfReader(str(fp))
        except ImportError:
            logger.debug('%s 不可用，继续尝试下一个解析库', mod)
            continue
        except Exception as e:
            logger.warning('%s 解析 %s 失败: %s', mod, fp.name, e)
            return None, 0
        pages = len(reader.pages)
        return '\n\n'.join((p.extract_text() or '') for p in reader.pages), pages
    return None, 0


def _extract_docx(fp):
    """Word 正文 + 表格抽取（python-docx，本项目已随 openpyxl 一并可用）"""
    try:
        from docx import Document
        d = Document(str(fp))
        parts = [p.text for p in d.paragraphs if p.text and p.text.strip()]
        for ti, tb in enumerate(d.tables):
            rows = [' | '.join((c.text or '').strip() for c in row.cells) for row in tb.rows]
            if rows:
                parts.append(f'[表{ti + 1}]\n' + '\n'.join(rows))
        return '\n'.join(parts), 0
    except ImportError:
        return None, 0
    except Exception as e:
        logger.warning('python-docx 解析 %s 失败: %s', fp.name, e)
        return None, 0


def _extract_doc(fp):
    """按后缀抽取文档正文。返回 (text|None, pages)。None 表示缺库或解析失败。"""
    ext = fp.suffix.lower()
    if ext == '.pdf':
        return _extract_pdf(fp)
    if ext == '.docx':
        return _extract_docx(fp)
    return _read_text_file(fp), 0


def _load_doc_as_text(file_name, fp):
    """文档类资料的索引文本。

    出境开启：正文（截断到 _MAX_DOC_CHARS）；
    出境关闭：只给元信息（文件名/类型/体积/字符数），不含任何原文片段。
    解析库缺失时同样只给元信息，并在文本里写明原因——让 Agent 知道"没读到"而不是"没有内容"。
    """
    head = [f"文件: {file_name}（文档类资料）"]
    try:
        size_kb = max(1, fp.stat().st_size // 1024)
    except OSError:
        size_kb = 0
    text, pages = _extract_doc(fp)
    if pages:
        head.append(f"页数: {pages}")
    if text is None:
        head.append('解析: 未成功——当前环境缺少该格式的解析库（PDF 需 pypdf 或 PyMuPDF）。'
                    '正文未进入索引，请勿凭空推断其内容。')
        return '\n'.join(head)
    text = text.strip()
    if _mode() != 'sample':
        head.append(f"体积: {size_kb} KB，正文 {len(text)} 字符")
        head.append('（仅本地统计模式：文档正文未随提问发送，仅下列元信息可用）')
        return '\n'.join(head)
    head.append(f"体积: {size_kb} KB，正文 {len(text)} 字符")
    head.append(text[:_MAX_DOC_CHARS])
    return '\n'.join(head)


def _load_as_text(file_name):
    """把上传文件抽取为便于检索的纯文本；失败返回空字符串。"""
    try:
        from core_config import load_dataframe, OUT_DIR
        from pathlib import Path
        if not file_name:
            return ""
        fp = Path(OUT_DIR) / file_name
        if not fp.exists():
            return ""
        if fp.suffix.lower() in _DOC_EXTS:
            return _load_doc_as_text(file_name, fp)
        df = load_dataframe(str(fp))
        if df is None or df.empty:
            return ""
        parts = [f"文件: {file_name}（{df.shape[0]} 行 × {df.shape[1]} 列）"]
        # 列名即"讲了什么"的骨架
        parts.append("列: " + ", ".join(str(c) for c in df.columns))
        if _mode() == 'sample':
            # 为控制 token，抽样行转文本（最多 40 行）
            sample = df.head(40) if df.shape[0] > 40 else df
            parts.append(sample.to_string(index=False)[:6000])
        else:
            parts.append(_stats_only(df))
        return "\n".join(parts)
    except Exception as e:
        logger.debug("文件转文本失败: %s", e)
        return ""


def _drop_index(file_name):
    """剔除某文件的旧索引（开关切换后必须重建，否则旧样本仍可被检索到）"""
    info = _INDEXED.pop(file_name, None)
    if not info:
        return
    try:
        from rag_unified import rag_engine
        rag_engine.delete_document(info['doc_id'])
    except Exception as e:
        logger.warning("剔除旧索引失败（可能残留过期样本）: %s", e)


def _fingerprint(file_name):
    """文件内容指纹（体积+修改时间）。同名文件被"改内容重传"后必须能触发重建。

    只用 文件名+模式 做幂等键会漏掉最常见的一种情况：用户改了数据、文件名没变
    （覆盖上传）。此时索引永远停留在旧内容上，检索命中与文件实际内容不一致。
    """
    try:
        from core_config import OUT_DIR
        from pathlib import Path
        st = (Path(OUT_DIR) / file_name).stat()
        return f'{st.st_size}:{int(st.st_mtime)}'
    except OSError as e:
        logger.debug('取文件指纹失败（按无指纹处理）: %s', e)
        return ''


def index_current_file(file_name):
    """把当前上传文件索引进全局 RAG 引擎。返回 doc_id 或 None。幂等（模式或内容变化则重建）。"""
    try:
        if not file_name:
            return None
        mode = _mode()
        fp = _fingerprint(file_name)
        info = _INDEXED.get(file_name)
        if info and info.get('mode') == mode and info.get('fp') == fp:
            return info['doc_id']
        if info:
            _drop_index(file_name)
        text = _load_as_text(file_name)
        if not text.strip():
            return None
        from rag_unified import rag_engine
        doc_id = rag_engine.add_document(text, file_name, "user_file")
        _INDEXED[file_name] = {'doc_id': doc_id, 'mode': mode, 'fp': fp}
        logger.info("已索引用户文件到 RAG: %s (%s, 模式=%s)", file_name, doc_id, mode)
        return doc_id
    except Exception as e:
        logger.warning("索引用户文件失败: %s", e)
        return None


def search_file_rag(query, top_k=4):
    """在已索引的用户文件中做混合语义检索。返回 [{score, file, text}] 列表。

    关闭样本出境后，来自「旧样本模式」文档的命中会被剔除（防止关闭后仍检索到样本行）。
    """
    if not _INDEXED:
        return []
    try:
        from rag_unified import rag_engine
        mode = _mode()
        stale = {i['doc_id'] for i in _INDEXED.values() if i.get('mode') != mode}
        results = rag_engine.search(query, top_k=top_k)
        return [{
            "score": round(r.final_score, 4),
            "file": r.chunk.doc_name,
            "text": r.chunk.text[:300],
        } for r in results if getattr(r.chunk, 'doc_id', None) not in stale]
    except Exception as e:
        logger.debug("文件 RAG 检索失败: %s", e)
        return []


def current_file_context(query=""):
    """供 agent system prompt 注入：已索引文件的存在提示 + query 相关片段。"""
    if not _INDEXED:
        return ""
    files = list(_INDEXED.keys())
    ctx = "你已可检索以下用户上传文件（调用 file_rag 工具获取相关内容）：" + "、".join(files)
    if _mode() == 'stats':
        ctx += ("\n注意：当前为「仅本地统计」模式，文件样本行未发送至模型，"
                "检索结果只有列结构与汇总量；如需样本行请让用户在设置中开启。")
    if query:
        hits = search_file_rag(query, top_k=2)
        if hits:
            segs = [f"[{h['file']}](score {h['score']}) {h['text']}" for h in hits]
            ctx += "\n命中的相关内容片段：\n" + "\n".join(segs)
    return ctx


def _indexed_files():
    return list(_INDEXED.keys())


def egress_status():
    """供面板/接口读取的当前文件出境状态（含默认值与关闭原因）"""
    enabled = file_sample_egress_enabled()
    return {'enabled': enabled, 'mode': _mode(), 'indexed': len(_INDEXED),
            'note': '' if enabled else disabled_reason('file_sample_egress')}
