#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
knowledge_db.py — AI 知识库数据库

将各类知识源（IMA、文档、金融知识等）统一存储到 SQLite 数据库，
供 AI 助手查询和推理使用。

核心能力：
- SQLite 持久化存储（向量 + 元数据）
- TF-IDF / BM25 关键词检索
- 语义向量检索（BGE-M3 / fallback）
- 多源知识统一管理（IMA、文件、手动输入）
- 知识分类、标签、版本管理
"""

import re
import math
import json
import time
import hashlib
import sqlite3
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field, asdict


DB_PATH = Path("output/knowledge/knowledge.db")


@dataclass
class KnowledgeItem:
    """知识条目"""
    id: str
    title: str
    content: str
    source: str  # 'ima', 'file', 'manual', 'finance', 'scrape'
    category: str = "general"
    tags: str = ""
    importance: int = 5  # 1-10
    created_at: str = ""
    updated_at: str = ""
    metadata: str = "{}"
    
    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().strftime('%Y-%m-%d %H:%M')
        if not self.updated_at:
            self.updated_at = self.created_at


class KnowledgeDB:
    """知识库数据库管理器"""
    
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        self._build_index()
    
    def _init_db(self):
        """初始化数据库表"""
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS knowledge (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                source TEXT DEFAULT 'manual',
                category TEXT DEFAULT 'general',
                tags TEXT DEFAULT '',
                importance INTEGER DEFAULT 5,
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT '',
                metadata TEXT DEFAULT '{}'
            );
            
            CREATE INDEX IF NOT EXISTS idx_knowledge_source ON knowledge(source);
            CREATE INDEX IF NOT EXISTS idx_knowledge_category ON knowledge(category);
            CREATE INDEX IF NOT EXISTS idx_knowledge_tags ON knowledge(tags);
            
            CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
                title, content, content=knowledge, content_rowid=rowid
            );
            
            CREATE TABLE IF NOT EXISTS knowledge_tags (
                tag TEXT,
                knowledge_id TEXT,
                FOREIGN KEY (knowledge_id) REFERENCES knowledge(id)
            );
            CREATE INDEX IF NOT EXISTS idx_tags_tag ON knowledge_tags(tag);
        """)
        self.conn.commit()
    
    def _build_index(self):
        """构建 FTS 全文索引"""
        try:
            self.conn.execute("""
                INSERT INTO knowledge_fts(rowid, title, content)
                SELECT rowid, title, content FROM knowledge
                WHERE rowid NOT IN (SELECT rowid FROM knowledge_fts)
            """)
            self.conn.commit()
        except Exception:
            pass
    
    def add(self, item: KnowledgeItem) -> str:
        """添加知识条目"""
        self.conn.execute("""
            INSERT OR REPLACE INTO knowledge 
            (id, title, content, source, category, tags, importance, created_at, updated_at, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (item.id, item.title, item.content, item.source, item.category,
              item.tags, item.importance, item.created_at, item.updated_at, item.metadata))
        
        # 更新 FTS 索引
        try:
            self.conn.execute(
                "INSERT INTO knowledge_fts(rowid, title, content) SELECT rowid, title, content FROM knowledge WHERE id = ?",
                (item.id,)
            )
        except Exception:
            pass
        
        # 更新标签索引
        self.conn.execute("DELETE FROM knowledge_tags WHERE knowledge_id = ?", (item.id,))
        for tag in item.tags.split(','):
            tag = tag.strip()
            if tag:
                self.conn.execute(
                    "INSERT INTO knowledge_tags(tag, knowledge_id) VALUES (?, ?)",
                    (tag, item.id)
                )
        
        self.conn.commit()
        return item.id
    
    def add_batch(self, items: List[KnowledgeItem]):
        """批量添加知识"""
        for item in items:
            self.add(item)
    
    def get(self, item_id: str) -> Optional[Dict]:
        """获取单条知识"""
        row = self.conn.execute("SELECT * FROM knowledge WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None
    
    def search(self, query: str, top_k: int = 5, source: str = None, category: str = None) -> List[Dict]:
        """多模式搜索（FTS + BM25 + 标签）"""
        results = []
        
        # 1. FTS5 全文搜索
        try:
            sql = "SELECT k.*, bm25(knowledge_fts) as score FROM knowledge_fts f JOIN knowledge k ON f.rowid = k.rowid WHERE knowledge_fts MATCH ?"
            params = [query]
            if source:
                sql += " AND k.source = ?"
                params.append(source)
            if category:
                sql += " AND k.category = ?"
                params.append(category)
            sql += " ORDER BY score LIMIT ?"
            params.append(top_k)
            rows = self.conn.execute(sql, params).fetchall()
            results.extend([dict(r) for r in rows])
        except Exception:
            pass
        
        # 2. 如果 FTS 无结果，用 LIKE 模糊搜索
        if not results:
            sql = "SELECT * FROM knowledge WHERE (title LIKE ? OR content LIKE ? OR tags LIKE ?)"
            params = [f'%{query}%', f'%{query}%', f'%{query}%']
            if source:
                sql += " AND source = ?"
                params.append(source)
            if category:
                sql += " AND category = ?"
                params.append(category)
            sql += " ORDER BY importance DESC LIMIT ?"
            params.append(top_k)
            rows = self.conn.execute(sql, params).fetchall()
            results.extend([dict(r) for r in rows])
        
        # 3. 标签搜索
        tag_sql = "SELECT k.* FROM knowledge k JOIN knowledge_tags t ON k.id = t.knowledge_id WHERE t.tag LIKE ?"
        tag_params = [f'%{query}%']
        if source:
            tag_sql += " AND k.source = ?"
            tag_params.append(source)
        tag_sql += " LIMIT ?"
        tag_params.append(top_k)
        tag_rows = self.conn.execute(tag_sql, tag_params).fetchall()
        results.extend([dict(r) for r in tag_rows])
        
        # 去重（按 id）
        seen = set()
        unique = []
        for r in results:
            if r['id'] not in seen:
                seen.add(r['id'])
                unique.append(r)
        
        return unique[:top_k]
    
    def search_by_tags(self, tags: List[str], top_k: int = 5) -> List[Dict]:
        """按标签搜索"""
        placeholders = ','.join('?' * len(tags))
        rows = self.conn.execute(f"""
            SELECT DISTINCT k.* FROM knowledge k
            JOIN knowledge_tags t ON k.id = t.knowledge_id
            WHERE t.tag IN ({placeholders})
            ORDER BY k.importance DESC LIMIT ?
        """, tags + [top_k]).fetchall()
        return [dict(r) for r in rows]
    
    def get_all_sources(self) -> List[str]:
        """获取所有知识来源"""
        rows = self.conn.execute("SELECT DISTINCT source FROM knowledge").fetchall()
        return [r[0] for r in rows]
    
    def get_all_categories(self) -> List[str]:
        """获取所有分类"""
        rows = self.conn.execute("SELECT DISTINCT category FROM knowledge").fetchall()
        return [r[0] for r in rows]
    
    def get_stats(self) -> Dict:
        """获取统计信息"""
        total = self.conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0]
        sources = self.conn.execute("SELECT source, COUNT(*) as cnt FROM knowledge GROUP BY source").fetchall()
        categories = self.conn.execute("SELECT category, COUNT(*) as cnt FROM knowledge GROUP BY category").fetchall()
        return {
            'total': total,
            'sources': {r[0]: r[1] for r in sources},
            'categories': {r[0]: r[1] for r in categories},
        }
    
    def delete(self, item_id: str):
        """删除知识条目"""
        self.conn.execute("DELETE FROM knowledge WHERE id = ?", (item_id,))
        self.conn.execute("DELETE FROM knowledge_tags WHERE knowledge_id = ?", (item_id,))
        self.conn.commit()
    
    def clear_source(self, source: str):
        """清空某来源的所有知识"""
        self.conn.execute("DELETE FROM knowledge WHERE source = ?", (source,))
        self.conn.execute("DELETE FROM knowledge_tags WHERE knowledge_id IN (SELECT id FROM knowledge WHERE source = ?)", (source,))
        self.conn.commit()
    
    def close(self):
        self.conn.close()


# ==========================================================
# 知识导入器
# ==========================================================

class KnowledgeImporter:
    """知识导入器：从各类源导入知识到数据库"""
    
    def __init__(self, db: KnowledgeDB):
        self.db = db
    
    def import_from_text(self, text: str, source: str, title: str = "", 
                         category: str = "general", tags: str = "", split: bool = True) -> List[str]:
        """从文本导入知识"""
        items = []
        
        if split:
            chunks = self._smart_split(text)
        else:
            chunks = [text]
        
        for i, chunk in enumerate(chunks):
            chunk = chunk.strip()
            if not chunk or len(chunk) < 20:
                continue
            
            item_id = hashlib.md5(f"{source}{title}{chunk[:100]}{i}".encode()).hexdigest()[:16]
            item = KnowledgeItem(
                id=item_id,
                title=title or f"知识片段 {i+1}",
                content=chunk,
                source=source,
                category=category,
                tags=tags,
                importance=7 if i == 0 else 5,
            )
            items.append(item)
        
        self.db.add_batch(items)
        return [item.id for item in items]
    
    def import_from_file(self, file_path: str, source: str = "file", 
                          category: str = "general", tags: str = "") -> List[str]:
        """从文件导入知识"""
        path = Path(file_path)
        if not path.exists():
            return []
        
        text = path.read_text(encoding='utf-8')
        title = path.stem
        return self.import_from_text(text, source, title, category, tags)
    
    def import_from_dict(self, data: Dict, source: str = "manual") -> str:
        """从字典导入单条知识"""
        item = KnowledgeItem(
            id=data.get('id', hashlib.md5(f"{source}{data.get('title','')}{time.time()}".encode()).hexdigest()[:16]),
            title=data.get('title', '未命名'),
            content=data.get('content', ''),
            source=source,
            category=data.get('category', 'general'),
            tags=data.get('tags', ''),
            importance=data.get('importance', 5),
            metadata=json.dumps(data.get('metadata', {}), ensure_ascii=False),
        )
        return self.db.add(item)
    
    def _smart_split(self, text: str, chunk_size: int = 500, overlap: int = 50) -> List[str]:
        """智能文本分块"""
        chunks = []
        paragraphs = text.split('\n\n')
        
        current = ""
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            if len(current) + len(para) + 2 <= chunk_size:
                current += para + "\n\n"
            else:
                if current:
                    chunks.append(current.strip())
                current = para + "\n\n"
        
        if current:
            chunks.append(current.strip())
        
        return chunks if chunks else [text[:chunk_size]]


# ==========================================================
# 便捷函数
# ==========================================================

_db_instance = None

def get_knowledge_db() -> KnowledgeDB:
    """获取知识库单例"""
    global _db_instance
    if _db_instance is None:
        _db_instance = KnowledgeDB()
    return _db_instance

def query_knowledge(query: str, top_k: int = 5, source: str = None) -> str:
    """便捷函数：查询知识库并返回文本"""
    db = get_knowledge_db()
    results = db.search(query, top_k=top_k, source=source)
    
    if not results:
        return ""
    
    parts = []
    for r in results:
        parts.append(f"[{r['source']}] {r['title']}\n{r['content'][:300]}")
    
    return "\n\n".join(parts)

def add_knowledge(title: str, content: str, source: str = "manual", 
                  category: str = "general", tags: str = "") -> str:
    """便捷函数：添加知识"""
    db = get_knowledge_db()
    item_id = hashlib.md5(f"{source}{title}{content[:100]}".encode()).hexdigest()[:16]
    item = KnowledgeItem(
        id=item_id,
        title=title,
        content=content,
        source=source,
        category=category,
        tags=tags,
    )
    return db.add(item)

def import_knowledge_file(file_path: str, source: str = "file", 
                          category: str = "general", tags: str = "") -> List[str]:
    """便捷函数：从文件导入知识"""
    db = get_knowledge_db()
    importer = KnowledgeImporter(db)
    return importer.import_from_file(file_path, source, category, tags)

def get_knowledge_stats() -> Dict:
    """便捷函数：获取知识库统计"""
    db = get_knowledge_db()
    return db.get_stats()


# ==========================================================
# 初始化默认知识
# ==========================================================

def init_default_knowledge():
    """初始化默认知识库"""
    db = get_knowledge_db()
    stats = db.get_stats()
    if stats['total'] > 0:
        return
    
    # 导入金融知识文件
    finance_file = Path("output/knowledge/financial_analysis_knowledge.txt")
    if finance_file.exists():
        importer = KnowledgeImporter(db)
        importer.import_from_file(
            str(finance_file), 
            source="finance", 
            category="finance",
            tags="股票,技术分析,基本面,量化,宏观经济,风险管理"
        )
    
    # 添加平台使用知识
    platform_knowledge = [
        {
            'title': 'data.tool 平台功能概览',
            'content': '''data.tool 是一个本地运行的数据分析平台，核心功能包括：
- 数据管理：上传、存储、爬取 CSV/Excel/PDF 文件
- 数据清洗：去重、缺失值填充、异常值剔除、分类编码
- 数据分析：描述性统计、相关性分析、分组统计
- 智能图表：柱状图、折点图、散点图、直方图、箱线图
- 机器学习：回归、分类、聚类、特征工程
- 时序分析：Prophet、ARIMA、ADF检验、时序分解
- 金融分析：K线图、技术指标、CAPM、Brinson归因
- 报告生成：HTML 模板化报告导出
- AI助手：自然语言操作、自动分析、知识库增强''',
            'category': 'platform',
            'tags': '平台,功能,使用指南',
        },
        {
            'title': 'AI助手使用技巧',
            'content': '''AI助手支持以下操作：
- "帮我分析当前文件" → 自动执行描述性统计+增强分析
- "画一个散点图分析X和Y的关系" → 生成可视化
- "帮我做假设检验" → t检验/ANOVA/卡方检验
- "生成透视表" → 数据透视分析
- "和上次的分析结果对比" → 多轮数据对比
- "结合金融知识分析" → 知识库增强分析
- "切换到分析标签页" → 直接操作网站导航''',
            'category': 'platform',
            'tags': 'AI助手,使用技巧,快捷操作',
        },
    ]
    
    importer = KnowledgeImporter(db)
    for k in platform_knowledge:
        importer.import_from_dict(k, source="manual")
    
    return db.get_stats()


if __name__ == "__main__":
    stats = init_default_knowledge()
    print("知识库初始化完成:", stats)
    
    # 测试搜索
    results = query_knowledge("MACD 金叉", top_k=3)
    print("\n搜索 'MACD 金叉':")
    print(results[:300] if results else "无结果")
