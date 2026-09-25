#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_markdown_kb.py — Markdown 目录批量导入内置知识点库
（替代"仅能通过 gen_knowledge.py 生成"的单一方式）

用法：
    import_markdown_dir("path/to/md_dir", category="finance")
    - 扫描目录下 *.md，解析 frontmatter（--- name/desc/category/tab ---）或回退文件名/首标题
    - 增量合并到 static/knowledge_data.json（同名条目默认跳过，overwrite=True 覆盖）
    - 导入后自动刷新 knowledge_rag 缓存
"""
import os
import re
import json
import logging
from pathlib import Path

logger = logging.getLogger('data.tool')

_KB_PATH = Path(__file__).parent / "static" / "knowledge_data.json"
_DETAIL_MAX = 3000  # detail 字段最大字符数


def _parse_frontmatter(text: str):
    """解析 frontmatter：返回 (meta_dict, body)。无 frontmatter 时返回 ({}, text)"""
    if not text.startswith('---'):
        return {}, text
    parts = text.split('---', 2)
    if len(parts) < 3:
        return {}, text
    front, body = parts[1], parts[2]
    meta = {}
    for line in front.strip().splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            meta[k.strip().lower()] = v.strip().strip('"\'')
    return meta, body.strip()


def _first_heading(text: str) -> str:
    """取首个 # 标题"""
    m = re.search(r'^#\s+(.+)$', text, flags=re.MULTILINE)
    return m.group(1).strip() if m else ''


def _summary(text: str, limit: int = 120) -> str:
    """取正文首段作为摘要"""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    s = lines[0] if lines else ''
    if len(s) > limit:
        s = s[:limit] + '…'
    return s


def import_markdown_dir(dir_path: str, category: str = None,
                        overwrite: bool = False, recursive: bool = False) -> dict:
    """批量导入目录下的 Markdown 到内置知识点库。返回统计。"""
    base = Path(dir_path)
    if not base.exists() or not base.is_dir():
        raise ValueError(f"目录不存在: {dir_path}")

    if recursive:
        files = sorted(base.rglob('*.md'))
    else:
        files = sorted(base.glob('*.md'))
    if not files:
        return {'imported': 0, 'skipped': 0, 'total': 0, 'files': [], 'message': '目录下没有 .md 文件'}

    # 加载现有条目
    if _KB_PATH.exists():
        try:
            entries = json.loads(_KB_PATH.read_text(encoding='utf-8'))
        except Exception as e:
            raise ValueError(f"现有 knowledge_data.json 解析失败: {e}")
        if not isinstance(entries, list):
            entries = []
    else:
        entries = []

    by_name = {e.get('name', ''): i for i, e in enumerate(entries)}
    imported, skipped = [], []

    for md in files:
        try:
            text = md.read_text(encoding='utf-8', errors='replace')
        except Exception as e:
            logger.warning(f"读取 {md.name} 失败: {e}")
            skipped.append({'file': md.name, 'reason': str(e)})
            continue

        meta, body = _parse_frontmatter(text)
        name = (meta.get('name') or '').strip() or _first_heading(body) or md.stem
        if not name:
            skipped.append({'file': md.name, 'reason': '无标题'})
            continue

        entry = {
            'name': name,
            'category': (meta.get('category') or category or 'md_import').strip(),
            'tab': (meta.get('tab') or '知识库').strip(),
            'desc': (meta.get('desc') or _summary(body)).strip(),
            'detail': body[:_DETAIL_MAX].strip(),
        }

        if name in by_name and not overwrite:
            skipped.append({'file': md.name, 'reason': '同名条目已存在（overwrite=False）'})
            continue

        if name in by_name:
            entries[by_name[name]] = entry          # 覆盖
        else:
            by_name[name] = len(entries)
            entries.append(entry)                   # 追加
        imported.append({'file': md.name, 'name': name})

    # 写回
    _KB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KB_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')

    # 刷新 RAG 缓存
    try:
        import knowledge_rag
        knowledge_rag._kb_cache = None
    except Exception:
        pass

    return {
        'imported': len(imported),
        'skipped': len(skipped),
        'total': len(files),
        'entries_total': len(entries),
        'files': imported,
        'skipped_detail': skipped[:20],
    }
