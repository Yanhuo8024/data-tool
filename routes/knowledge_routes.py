"""
routes/knowledge_routes.py — 知识库管理路由（企业知识库）
"""
from core_config import exc_response
import json
from flask import Blueprint, request, jsonify, g
from models import db, KnowledgeBase, KnowledgeDoc
from auth import require_auth_or_api_key_local, get_current_user

knowledge_bp = Blueprint('knowledge', __name__)


@knowledge_bp.route('', methods=['GET'])
@require_auth_or_api_key_local
def list_knowledge_bases():
    """列出用户的知识库"""
    user = get_current_user()
    kbs = KnowledgeBase.query.filter_by(user_id=user.id).all()
    return jsonify({'ok': True, 'knowledge_bases': [{
        'id': kb.id, 'name': kb.name, 'description': kb.description,
        'doc_count': kb.doc_count,
        'created_at': kb.created_at.isoformat() if kb.created_at else None
    } for kb in kbs]})


@knowledge_bp.route('', methods=['POST'])
@require_auth_or_api_key_local
def create_knowledge_base():
    """创建知识库"""
    user = get_current_user()
    data = request.get_json()
    if not data:
        return jsonify({'error': '无效的请求数据'}), 400
    
    kb = KnowledgeBase(
        user_id=user.id,
        name=data.get('name', '新知识库'),
        description=data.get('description', '')
    )
    db.session.add(kb)
    db.session.commit()
    return jsonify({'ok': True, 'knowledge_base': {'id': kb.id, 'name': kb.name}}), 201


@knowledge_bp.route('/<kb_id>', methods=['DELETE'])
@require_auth_or_api_key_local
def delete_knowledge_base(kb_id):
    """删除知识库"""
    user = get_current_user()
    kb = KnowledgeBase.query.filter_by(id=kb_id, user_id=user.id).first()
    if not kb:
        return jsonify({'error': '知识库不存在'}), 404
    KnowledgeDoc.query.filter_by(kb_id=kb_id).delete()
    db.session.delete(kb)
    db.session.commit()
    return jsonify({'ok': True})


@knowledge_bp.route('/<kb_id>/docs', methods=['GET'])
@require_auth_or_api_key_local
def list_docs(kb_id):
    """列出文档"""
    user = get_current_user()
    kb = KnowledgeBase.query.filter_by(id=kb_id, user_id=user.id).first()
    if not kb:
        return jsonify({'error': '知识库不存在'}), 404
    docs = KnowledgeDoc.query.filter_by(kb_id=kb_id).all()
    return jsonify({'ok': True, 'docs': [{
        'id': d.id, 'title': d.title, 'file_type': d.file_type,
        'chunk_count': d.chunk_count,
        'created_at': d.created_at.isoformat() if d.created_at else None
    } for d in docs]})


def _format_entry(e: dict) -> str:
    """把知识点条目格式化为文档正文"""
    lines = [f"## {e.get('name', '')}"]
    if e.get('desc'):
        lines.append(f"简介：{e['desc']}")
    if e.get('detail'):
        lines.append(e['detail'])
    if e.get('formula'):
        lines.append(f"公式：{e['formula']}")
    if e.get('params'):
        lines.append("参数：" + "；".join(f"{p.get('k','')}={p.get('v','')}" for p in e['params']))
    if e.get('scenarios'):
        lines.append("适用场景：" + "；".join(e['scenarios']))
    if e.get('pitfalls'):
        lines.append("常见踩坑：" + "；".join(e['pitfalls']))
    if e.get('tips'):
        lines.append("注意事项：" + "；".join(e['tips']))
    if e.get('cases'):
        lines.append("实战案例：" + e['cases'])
    if e.get('related'):
        lines.append("相关概念：" + "、".join(e['related']))
    return "\n".join(lines)


@knowledge_bp.route('/<kb_id>/docs', methods=['POST'])
@require_auth_or_api_key_local
def add_doc(kb_id):
    """添加文档（multipart 文件 或 JSON 文本）"""
    user = get_current_user()
    kb = KnowledgeBase.query.filter_by(id=kb_id, user_id=user.id).first()
    if not kb:
        return jsonify({'error': '知识库不存在'}), 404

    title = request.form.get('title', '') or request.values.get('title', '')
    content = ''
    file_type = 'text'

    if 'file' in request.files:
        f = request.files['file']
        title = title or (f.filename or '未命名')
        ext = (f.filename or '').rsplit('.', 1)[-1].lower() if '.' in (f.filename or '') else ''
        file_type = ext or 'text'
        try:
            raw = f.read()
            try:
                content = raw.decode('utf-8')
            except UnicodeDecodeError:
                content = raw.decode('gbk', errors='ignore')
        except Exception as e:
            return jsonify({'error': f'读取文件失败: {e}'}), 400
    else:
        data = request.get_json(silent=True) or {}
        title = title or data.get('title', '未命名')
        content = data.get('content', '')
        file_type = data.get('file_type', 'text')

    content = (content or '').strip()
    if not content:
        return jsonify({'error': '文档内容为空'}), 400

    from rag_unified import chunk_text
    chunk_count = len(chunk_text(content, 300, 50))

    doc = KnowledgeDoc(
        kb_id=kb_id, user_id=user.id,
        title=title, content=content,
        file_type=file_type, chunk_count=chunk_count,
    )
    db.session.add(doc)
    kb.doc_count = (kb.doc_count or 0) + 1
    db.session.commit()
    return jsonify({'ok': True, 'doc': {'id': doc.id, 'title': doc.title, 'chunk_count': chunk_count}}), 201


@knowledge_bp.route('/sync-builtin', methods=['POST'])
@require_auth_or_api_key_local
def sync_builtin_knowledge():
    """把内置知识点库（金融/银行/税务等 104 条）一键同步到企业知识库"""
    import os
    user = get_current_user()
    json_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             'static', 'knowledge_data.json')
    if not os.path.exists(json_path):
        return jsonify({'error': '知识点数据文件不存在，请先运行 gen_knowledge.py'}), 500
    with open(json_path, encoding='utf-8') as f:
        entries = json.load(f)
    if not entries:
        return jsonify({'error': '知识点数据为空'}), 500

    kb_name = '内置知识库·金融银行税务'
    kb = KnowledgeBase.query.filter_by(user_id=user.id, name=kb_name).first()
    if not kb:
        kb = KnowledgeBase(user_id=user.id, name=kb_name,
                           description='由内置知识点库自动同步（金融/银行/信贷/税收等 %d 条）' % len(entries))
        db.session.add(kb)
        db.session.commit()
    else:
        # 幂等：清空旧文档后重写
        KnowledgeDoc.query.filter_by(kb_id=kb.id).delete()

    cat_name = {'bank': '银行与信贷', 'tax': '税收政策', 'finance': '金融数据',
                'ml': '机器学习', 'stats': '统计分析', 'ts': '时序分析'}
    for e in entries:
        cat = cat_name.get(e.get('category'), e.get('tab', '通用'))
        content = _format_entry(e)
        from rag_unified import chunk_text
        doc = KnowledgeDoc(
            kb_id=kb.id, user_id=user.id,
            title=f"[{cat}] {e.get('name', '')}",
            content=content,
            file_type='text',
            chunk_count=len(chunk_text(content, 300, 50)),
        )
        db.session.add(doc)
    kb.doc_count = len(entries)
    db.session.commit()
    return jsonify({'ok': True, 'kb_id': kb.id, 'kb_name': kb.name, 'doc_count': len(entries)})


@knowledge_bp.route('/import-dir', methods=['POST'])
@require_auth_or_api_key_local
def import_markdown_dir_api():
    """Markdown 目录批量导入内置知识点库（替代仅 gen_knowledge.py）
    请求：{"dir": "路径", "category": "分类(可选)", "overwrite": false, "recursive": false}"""
    from import_markdown_kb import import_markdown_dir
    data = request.get_json(silent=True) or {}
    dir_path = (data.get('dir') or '').strip()
    if not dir_path:
        return jsonify({'ok': False, 'error': '缺少 dir 参数'}), 400
    try:
        result = import_markdown_dir(
            dir_path,
            category=(data.get('category') or '').strip() or None,
            overwrite=bool(data.get('overwrite', False)),
            recursive=bool(data.get('recursive', False)),
        )
        return jsonify({'ok': True, **result})
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        import traceback
        traceback.print_exc()
        return exc_response(e)
