#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
routes/file_routes.py — 文件管理、报告浏览、快照（从 system_routes 拆分）
"""
from core_config import exc_response
import json
import traceback
from pathlib import Path
from datetime import datetime

from flask import request, jsonify, send_file, Blueprint
from core_config import (
    OUT_DIR, pd, os, glob, re, time, logger,
    UPLOAD_DIR, df_cache, load_dataframe, safe_data_path,
)
from auth import local_guard
from data_lineage import lineage_mgr


def _invalidate_tool_cache(*paths):
    """P1-1：删除/回滚文件后同步失效 agent 的工具结果缓存。

    旧实现只失效了 `df_cache`（DataFrame 缓存），遗漏 `ai_agent.tool_cache`，
    使 describe/overview/columns 等在 TTL 内仍返回已删除/旧版文件的结构信息。
    """
    try:
        import ai_agent as _ai
        for _p in paths:
            if _p:
                _ai.tool_cache.invalidate_file(Path(_p).name)
    except Exception as _e:
        logger.debug('工具缓存失效跳过: %s', _e)


def register_routes(app, limiter):
    bp = Blueprint('file_routes', __name__)
    # 高危面（文件浏览/下载/删除/改名/快照回滚）：远程请求必须认证
    bp.before_request(local_guard)

    def _guard_path(raw):
        """用户输入路径 → 校验并返回绝对 Path；越界抛 PermissionError"""
        return safe_data_path(raw)

    def _guard_basename(raw):
        """仅接受纯文件名（不允许目录分隔符/.. /绝对路径），返回纯文件名"""
        s = str(raw).strip()
        if not s or '..' in s or '/' in s or '\\' in s or ':' in s:
            raise PermissionError(f"非法文件名: {raw}")
        name = Path(s).name
        if not name or name.startswith('.deleted_') or name.startswith('.'):
            raise PermissionError(f"非法文件名: {raw}")
        return name

    # ---- 文件标签（个人工作空间：按项目/分类组织数据集） ----
    _TAGS_FILE = OUT_DIR / ".file_tags.json"

    # ---- 已删除清单（沙箱禁止 unlink/rename 时的元数据软删除，可恢复） ----
    _DELETED_INDEX = OUT_DIR / ".deleted_files.json"

    def _load_deleted_map():
        try:
            if _DELETED_INDEX.exists():
                return json.loads(_DELETED_INDEX.read_text(encoding='utf-8'))
        except Exception:
            pass
        return {}

    def _save_deleted_map(m):
        try:
            _DELETED_INDEX.write_text(json.dumps(m, ensure_ascii=False), encoding='utf-8')
        except Exception:
            pass

    def _mark_deleted(path, soft=True):
        m = _load_deleted_map()
        m[str(path)] = {'ts': time.time(), 'name': Path(path).name, 'soft': soft}
        _save_deleted_map(m)

    def _unmark_deleted(path):
        m = _load_deleted_map()
        if str(path) in m:
            del m[str(path)]
            _save_deleted_map(m)
            return True
        return False

    def _load_file_tags():
        try:
            return json.load(open(_TAGS_FILE, encoding='utf-8'))
        except Exception:
            return {}

    def _save_file_tags(tags):
        try:
            json.dump(tags, open(_TAGS_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        except Exception:
            pass

    @bp.route('/api/files/tags', methods=['GET'])
    def api_file_tags_list():
        return jsonify({'ok': True, 'tags': _load_file_tags()})

    @bp.route('/api/files/tags', methods=['POST'])
    @limiter.limit("30 per minute")
    def api_file_tags_set():
        data = request.json or {}
        path = (data.get('path') or '').strip()
        tags = data.get('tags') or []
        if not path:
            return jsonify({'ok': False, 'error': '缺少 path'}), 400
        tags = [str(t).strip() for t in tags if str(t).strip()]
        tags_map = _load_file_tags()
        tags_map[path] = tags
        _save_file_tags(tags_map)
        return jsonify({'ok': True, 'path': path, 'tags': tags})

    # ============================================================
    #  文件管理 / 列表 / 搜索 / 删除
    # ============================================================

    @bp.route('/api/files', methods=['GET'])
    def api_files():
        """列出所有数据文件（过滤软删除标记 .deleted_）"""
        import glob
        patterns = ['*.csv', '*.xlsx', '*.xls', '*.json', '*.txt', '*.md', '*.html', '*.py', '*.png', '*.jpg', '*.svg', '*.xml']
        files = []
        tags_map = _load_file_tags()
        deleted_map = _load_deleted_map()
        for p in patterns:
            for f in glob.glob(str(OUT_DIR / '**' / p), recursive=True):
                try:
                    fp = Path(f)
                    if fp.is_file() and '.git' not in str(fp) and not fp.name.startswith('.deleted_') and str(fp) not in deleted_map:
                        files.append({
                            'name': fp.name,
                            'path': str(fp),
                            'size': fp.stat().st_size,
                            'mtime': fp.stat().st_mtime,
                            'ext': fp.suffix.lower(),
                            'tags': tags_map.get(str(fp), []),
                        })
                except Exception:
                    pass
        files.sort(key=lambda x: -x['mtime'])
        # 附带元数据软删除清单（供前端恢复入口展示）
        deleted_list = [{'name': v.get('name', Path(k).name), 'path': k, 'ts': v.get('ts', 0)}
                        for k, v in _load_deleted_map().items()][:100]
        return jsonify({'ok': True, 'files': files[:200], 'deleted': deleted_list})

    @bp.route('/api/files/browse', methods=['GET'])
    def api_files_browse():
        """浏览文件内容（前 100 行预览）"""
        file_path = request.args.get('path', '')
        if not file_path:
            return jsonify({'ok': False, 'error': '缺少 path 参数'}), 400
        try:
            fp = _guard_path(file_path)
        except PermissionError as e:
            return jsonify({'ok': False, 'error': str(e)}), 403
        if not fp.exists():
            return jsonify({'ok': False, 'error': '文件不存在'}), 404
        try:
            size = fp.stat().st_size
            if fp.suffix.lower() in ('.csv', '.txt', '.md', '.json', '.xml', '.py', '.html', '.log'):
                # 流式只读前 100 行：不能用 f.readlines()[:100]（会先把整个文件读进内存，
                # 遇到百 MB/GB 级文件会占满内存并长时间阻塞 worker）
                lines = []
                with open(fp, 'r', encoding='utf-8', errors='ignore') as f:
                    for i, l in enumerate(f):
                        if i >= 100:
                            break
                        lines.append(l.rstrip('\n\r')[:2000])   # 单行过长时截断，避免超大单元格撑爆响应
                return jsonify({'ok': True, 'content': '\n'.join(lines),
                                'total_lines': size, 'total_bytes': size,
                                'lines_shown': len(lines), 'truncated': True})
            elif fp.suffix.lower() in ('.xlsx', '.xls'):
                df = pd.read_excel(fp, nrows=100)
                return jsonify({'ok': True, 'content': df.to_csv(index=False), 'truncated': True})
            return jsonify({'ok': True, 'content': f'[文件类型: {fp.suffix}，不支持预览]'})
        except Exception as e:
            return exc_response(e)

    @bp.route('/api/files/download', methods=['GET'])
    def api_files_download():
        """下载文件"""
        file_path = request.args.get('path', '')
        if not file_path:
            return jsonify({'ok': False, 'error': '缺少 path 参数'}), 400
        try:
            fp = _guard_path(file_path)
        except PermissionError as e:
            return jsonify({'ok': False, 'error': str(e)}), 403
        if not fp.exists():
            return jsonify({'ok': False, 'error': '文件不存在'}), 404
        return send_file(fp, as_attachment=True)

    @bp.route('/api/files/delete', methods=['POST'])
    @limiter.limit("30 per minute")
    def api_files_delete():
        """删除文件（支持单个 path 或批量 paths）
        沙箱环境无法物理删除时降级为软删除（重命名 .deleted_ 前缀并从列表隐藏）"""
        data = request.json or {}
        paths = list(data.get('paths') or [])
        single = data.get('path', '')
        archive_only = bool(data.get('archive'))   # 归档 = 强制软删除（可恢复）
        if single:
            paths.append(single)
        if not paths:
            return jsonify({'ok': False, 'error': '缺少 path/paths 参数'}), 400
        deleted, soft, errors = [], [], []
        for p in paths:
            # 路径守卫：仅允许 OUT_DIR 内文件被删除
            try:
                p = str(_guard_path(p))
            except PermissionError as e:
                errors.append({'path': p, 'error': str(e)})
                continue
            # 归档模式：元数据软删除（沙箱禁止 rename 时仍可用，可从列表隐藏、可恢复）
            if archive_only:
                try:
                    fp = Path(p)
                    if not fp.exists() and str(fp) not in _load_deleted_map():
                        errors.append({'path': p, 'error': '文件不存在'})
                        continue
                    _mark_deleted(p, soft=True)
                    df_cache.invalidate(p)
                    _invalidate_tool_cache(p)
                    soft.append(p)
                except Exception as e3:
                    errors.append({'path': p, 'error': f'归档失败: {e3}'})
                continue
            try:
                os.remove(p)
                df_cache.invalidate(p)
                _invalidate_tool_cache(p)
                _unmark_deleted(p)
                deleted.append(p)
            except Exception as e1:
                # 物理删除失败（沙箱禁止 unlink / 回收站不可用）→ 元数据软删除：写入清单并从列表隐藏，可恢复
                try:
                    if not Path(p).exists() and str(p) not in _load_deleted_map():
                        errors.append({'path': p, 'error': '文件不存在'})
                        continue
                    _mark_deleted(p, soft=True)
                    df_cache.invalidate(p)
                    _invalidate_tool_cache(p)
                    soft.append(p)
                except Exception as e2:
                    errors.append({'path': p, 'error': f'{e1} / {e2}'})
        return jsonify({'ok': not errors, 'deleted': deleted, 'soft_deleted': soft, 'errors': errors})

    @bp.route('/api/files/restore', methods=['POST'])
    @limiter.limit("30 per minute")
    def api_files_restore():
        """恢复元数据软删除的文件（从已删除清单移除，重新出现在列表）"""
        data = request.json or {}
        paths = list(data.get('paths') or [])
        single = data.get('path', '')
        if single:
            paths.append(single)
        if not paths:
            return jsonify({'ok': False, 'error': '缺少 path/paths 参数'}), 400
        restored, errors = [], []
        for p in paths:
            try:
                p = str(_guard_path(p))
            except PermissionError as e:
                errors.append({'path': p, 'error': str(e)})
                continue
            if _unmark_deleted(p):
                restored.append(p)
            else:
                errors.append({'path': p, 'error': '不在已删除清单中'})
        return jsonify({'ok': not errors, 'restored': restored, 'errors': errors})

    @bp.route('/api/files/save', methods=['POST'])
    @limiter.limit("20 per minute")
    def api_files_save():
        """保存新文件（上传内容）"""
        data = request.json or {}
        filename = data.get('filename', 'untitled.txt')
        content = data.get('content', '')
        try:
            safe_name = _guard_basename(filename)
        except PermissionError as e:
            return jsonify({'ok': False, 'error': str(e)}), 403
        file_path = OUT_DIR / safe_name
        try:
            file_path.write_text(content, encoding='utf-8')
            try:
                lineage_mgr.record("save", "", filename, detail="新建文件")
            except Exception:
                pass
            return jsonify({'ok': True, 'file': str(file_path)})
        except Exception as e:
            return exc_response(e)

    @bp.route('/api/files/rename', methods=['POST'])
    @limiter.limit("10 per minute")
    def api_files_rename():
        data = request.json or {}
        old = data.get('old', '')
        new = data.get('new', '')
        if not old or not new:
            return jsonify({'ok': False, 'error': '参数缺失'}), 400
        try:
            old_p = _guard_path(old)
            # 新名仅允许纯文件名，落在与旧文件同一目录
            new_name = _guard_basename(new)
            new_p = old_p.parent / new_name
        except PermissionError as e:
            return jsonify({'ok': False, 'error': str(e)}), 403
        try:
            os.rename(str(old_p), str(new_p))
            return jsonify({'ok': True})
        except Exception as e:
            return exc_response(e)

    # ============================================================
    #  报告文件管理
    # ============================================================

    @bp.route("/api/report/file/<name>")
    def api_report_file(name):
        try:
            safe_name = _guard_basename(name)
        except PermissionError as e:
            return jsonify({"error": str(e)}), 403
        f = OUT_DIR / "reports" / safe_name
        if f.exists():
            return send_file(str(f), as_attachment=True, download_name=safe_name)
        return jsonify({"error": "文件不存在"}), 404

    @bp.route("/api/report/generate", methods=["POST"])
    @limiter.limit("10 per minute")
    def api_report_generate():
        try:
            from core_config import HAS_REPORT, save_report
            if not HAS_REPORT:
                return jsonify({"error": "report_generator 模块未加载"}), 500
            data = request.json
            file_path = safe_data_path(data["file"])
            if not file_path.exists():
                return jsonify({"error": "文件不存在"}), 400
            template = data.get("template", "data_exploration")
            html_path, meta = save_report(file_path, OUT_DIR, template=template)
            if not html_path:
                return jsonify(meta), 500
            try:
                lineage_mgr.record("report", data["file"], Path(html_path).name,
                                   detail=f"模板: {template}", ref="report")
            except Exception:
                pass
            return jsonify({"ok": True, "file": Path(html_path).name, "template": template})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/report/templates", methods=["GET"])
    def api_report_templates():
        try:
            from core_config import list_templates
            templates = list_templates()
            return jsonify({"ok": True, "templates": templates})
        except Exception as e:
            return exc_response(e)

    # ============================================================
    #  快照管理
    # ============================================================

    @bp.route('/api/snapshot/create', methods=['POST'])
    @limiter.limit("5 per minute")
    def api_snapshot_create():
        data = request.json or {}
        file_name = data.get('file', '')
        if not file_name:
            return jsonify({'ok': False, 'error': '缺少 file 参数'}), 400
        try:
            # snapshot_mgr 为 data_lineage 模块级单例；create_snapshot 需要文件真实路径
            from data_lineage import snapshot_mgr
            target = _guard_path(file_name)
            if not target.exists():
                return jsonify({'ok': False, 'error': f'文件不存在: {file_name}'}), 404
            snap = snapshot_mgr.create_snapshot(
                str(target), operation='manual', description=f'手动快照 {file_name}')
            if not snap:
                return jsonify({'ok': False, 'error': '快照创建失败'}), 500
            return jsonify({'ok': True, 'sha': snap.id, 'snapshot': snap.to_dict()})
        except Exception as e:
            return exc_response(e)

    @bp.route('/api/snapshot/rollback', methods=['POST'])
    @limiter.limit("3 per minute")
    def api_snapshot_rollback():
        data = request.json or {}
        file_name = data.get('file', '')
        sha = data.get('sha', '')
        if not file_name or not sha:
            return jsonify({'ok': False, 'error': '缺少参数'}), 400
        try:
            from data_lineage import snapshot_mgr
            target = str(_guard_path(file_name))
            result = snapshot_mgr.rollback(sha, target)
            if not result.get('ok'):
                return jsonify({'ok': False, 'error': result.get('error', '回滚失败')}), 400
            df_cache.invalidate(target)  # 回滚后旧 DataFrame 缓存失效
            _invalidate_tool_cache(target)  # P1-1：工具缓存同步失效
            return jsonify({'ok': True, 'message': result.get('message', '')})
        except Exception as e:
            return exc_response(e)

    @bp.route('/api/snapshot/list', methods=['GET'])
    def api_snapshot_list():
        file_name = request.args.get('file', '').strip()
        try:
            from data_lineage import snapshot_mgr
            all_snaps = snapshot_mgr.list_snapshots()  # 按时间倒序
            # 传了 file 则只返回该文件的快照；否则返回全部
            if file_name:
                key = Path(file_name).name
                all_snaps = [s for s in all_snaps
                             if s.source_file == key or Path(s.file_path).name == key]
            return jsonify({'ok': True, 'snapshots': [s.to_dict() for s in all_snaps]})
        except Exception as e:
            return exc_response(e)
    app.register_blueprint(bp)