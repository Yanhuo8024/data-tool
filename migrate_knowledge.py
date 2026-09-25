#!/usr/bin/env python3
"""
migrate_knowledge.py — 将 RAG 知识库迁移到新知识库系统
"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')

# 初始化数据库
from app import app
from models import db, User, KnowledgeBase, KnowledgeDoc
from knowledge_db import get_knowledge_db

with app.app_context():
    # 1. 获取或创建默认用户
    user = User.query.filter_by(username='admin').first()
    if not user:
        user = User(username='admin', email='admin@data.tool', role='admin')
        user.set_password('admin123')
        db.session.add(user)
        db.session.commit()
        print(f'[用户] 创建管理员: {user.username}')
    else:
        print(f'[用户] 使用已有用户: {user.username}')
    
    # 2. 创建知识库
    kb = KnowledgeBase(
        user_id=user.id,
        name='RAG 知识库（迁移）',
        description='从原有 RAG 知识库迁移的全部知识条目'
    )
    db.session.add(kb)
    db.session.commit()
    print(f'[知识库] 创建: {kb.name}')
    
    # 3. 读取旧知识库所有条目
    old_db = get_knowledge_db()
    rows = old_db.conn.execute(
        'SELECT id, title, content, source, category, tags, importance, created_at, updated_at FROM knowledge'
    ).fetchall()
    print(f'[读取] 旧知识库共 {len(rows)} 条')
    
    # 4. 迁移
    count = 0
    for r in rows:
        doc = KnowledgeDoc(
            kb_id=kb.id,
            user_id=user.id,
            title=r[1] or '未命名',
            content=r[2] or '',
            file_type=r[3] or 'manual',
            chunk_count=r[6] if r[6] else 1,
        )
        db.session.add(doc)
        count += 1
    
    kb.doc_count = count
    db.session.commit()
    
    print(f'[完成] 迁移 {count} 条知识 → 知识库 "{kb.name}"')
    print(f'\n验证:')
    print(f'  知识库: {KnowledgeBase.query.count()} 个')
    print(f'  文档数: {KnowledgeDoc.query.count()} 篇')
    print(f'  用户: {User.query.count()} 个')
