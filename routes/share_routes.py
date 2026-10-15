#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
routes/share_routes.py — token 化只读分享链接（协作的轻量落地）
- POST /api/share/create {path} → 生成分享 token（HMAC 签名 + 过期时间）
- GET  /share/<token>        → 只读预览页（文件名/大小/内容预览/下载）
安全：token 由 SECRET_KEY HMAC 签名、可设置过期、仅只读，符合数据安全约束。
说明：真正的多人协同编辑需要多用户服务架构，单机部署以"只读分享"形式提供协作能力。
"""
from core_config import exc_response
import base64
import hashlib
import hmac
import time
import traceback
from datetime import datetime
from pathlib import Path

from flask import request, jsonify, render_template_string, Blueprint

from core_config import OUT_DIR, logger, pd, safe_data_path
from auth import require_auth_or_api_key_local


def _secret(app):
    return (app.config.get('SECRET_KEY') or 'share-default').encode()


def _sign(app, path: str, expiry: int) -> str:
    sig = hmac.new(_secret(app), f"{path}|{expiry}".encode(), hashlib.sha256).hexdigest()[:16]
    p64 = base64.urlsafe_b64encode(path.encode()).decode()
    return f"{sig}.{expiry}.{p64}"


def _verify(app, token: str):
    """校验 token，返回原始路径；非法/过期返回 None"""
    try:
        sig, expiry, p64 = token.split('.')
        expiry = int(expiry)
        if expiry < time.time():
            return None
        path = base64.urlsafe_b64decode(p64.encode()).decode()
        expect = hmac.new(_secret(app), f"{path}|{expiry}".encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(expect, sig):
            return None
        return path
    except Exception:
        return None


def register_routes(app, limiter):
    bp = Blueprint('share_routes', __name__)
    # ============================================================
    #  生成分享链接
    # ============================================================
    @bp.route("/api/share/create", methods=["POST"])
    @require_auth_or_api_key_local
    @limiter.limit("20 per minute")
    def api_share_create():
        try:
            data = request.get_json() or {}
            path = (data.get("path") or "").strip()
            ttl_hours = int(data.get("ttl_hours", 24))
            if not path:
                return jsonify({"ok": False, "error": "缺少 path 参数"}), 400
            # 路径守卫：仅允许分享 output/ 目录内文件（防分享任意磁盘文件）
            try:
                real = str(safe_data_path(path))
            except PermissionError as e:
                return jsonify({"ok": False, "error": str(e)}), 403
            fp = Path(real)
            if not fp.exists():
                return jsonify({"ok": False, "error": "文件不存在"}), 404
            expiry = int(time.time()) + ttl_hours * 3600
            token = _sign(app, real, expiry)
            url = f"/share/{token}"
            return jsonify({
                "ok": True,
                "url": url,
                "full_url": f"http://{request.host}{url}",
                "expires": datetime.fromtimestamp(expiry).strftime("%Y-%m-%d %H:%M"),
                "file": Path(real).name,
            })
        except Exception as e:
            logger.error(f"share create error: {e}", exc_info=True)
            return exc_response(e)

    # ============================================================
    #  只读分享页
    # ============================================================
    @bp.route("/share/<token>")
    def api_share_view(token):
        path = _verify(app, token)
        if not path:
            return render_template_string(_ERROR_PAGE, msg="链接无效或已过期", title="分享链接无效"), 403
        # 纵深防御：token 中的路径再次校验仍在 output/ 内（防旧 token/密钥轮换后越界）
        try:
            fp = safe_data_path(path)
        except PermissionError:
            return render_template_string(_ERROR_PAGE, msg="链接无效或已过期", title="分享链接无效"), 403
        if not fp.exists():
            return render_template_string(_ERROR_PAGE, msg="文件已不存在", title="分享链接无效"), 404
        try:
            size_mb = fp.stat().st_size / (1024 * 1024)
            suffix = fp.suffix.lower()
            preview_html = ""
            if suffix in ('.csv', '.txt', '.json', '.md'):
                try:
                    df = pd.read_csv(fp, nrows=20)
                    preview_html = df.head(20).to_html(
                        index=False, border=0,
                        classes="share-tbl",
                        na_rep="", escape=True)
                except Exception:
                    # XSS 修复：纯文本预览必须先 HTML 转义再进模板（|safe 原样注入）
                    from markupsafe import escape as _esc
                    preview_html = '<pre>' + str(_esc(fp.read_text(encoding='utf-8', errors='ignore')[:2000])) + '</pre>'
            elif suffix in ('.xlsx', '.xls'):
                try:
                    df = pd.read_excel(fp, nrows=20)
                    preview_html = df.head(20).to_html(
                        index=False, border=0,
                        classes="share-tbl",
                        na_rep="", escape=True)
                except Exception as e:
                    preview_html = f'<p>Excel 预览失败: {e}</p>'
            elif suffix in ('.png', '.jpg', '.jpeg', '.gif', '.svg'):
                preview_html = f'<img src="/api/files/download?path={path}" style="max-width:100%;border-radius:8px;">'
            elif suffix in ('.html', '.htm'):
                preview_html = f'<iframe src="/api/files/download?path={path}" style="width:100%;height:480px;border:1px solid #eee;border-radius:8px;"></iframe>'
            else:
                preview_html = f'<p style="color:#888;">该文件类型不支持预览，可下载查看。</p>'

            download_url = f"/api/files/download?path={path}"
            return render_template_string(_SHARE_PAGE, **{
                "title": f"分享 · {fp.name}",
                "filename": fp.name,
                "size": f"{size_mb:.2f} MB" if size_mb >= 1 else f"{fp.stat().st_size / 1024:.1f} KB",
                "mtime": datetime.fromtimestamp(fp.stat().st_mtime).strftime("%Y-%m-%d %H:%M"),
                "preview_html": preview_html,
                "download_url": download_url,
            })
        except Exception as e:
            logger.error(f"share view error: {e}", exc_info=True)
            return render_template_string(_ERROR_PAGE, msg=str(e), title="分享页错误"), 500
    app.register_blueprint(bp)


_SHARE_PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ title }}</title>
<style>
  body{font-family:'Microsoft YaHei',sans-serif;background:#f5f6fa;margin:0;padding:24px;color:#333}
  .card{max-width:900px;margin:0 auto;background:#fff;border-radius:14px;padding:24px;box-shadow:0 4px 20px rgba(0,0,0,.06)}
  h1{font-size:18px;margin:0 0 4px}
  .meta{color:#888;font-size:12px;margin-bottom:16px}
  .btn{display:inline-block;background:#FF6B6B;color:#fff;text-decoration:none;padding:8px 18px;border-radius:8px;font-size:13px}
  .share-tbl{border-collapse:collapse;width:100%;font-size:12px}
  .share-tbl th{background:#f8f9fc;text-align:left;padding:6px 8px;border-bottom:2px solid #eee}
  .share-tbl td{padding:6px 8px;border-bottom:1px solid #f0f0f0;white-space:nowrap}
  .wrap{overflow-x:auto}
  .foot{margin-top:18px;color:#bbb;font-size:11px;text-align:center}
</style></head><body>
<div class="card">
  <h1>📄 {{ filename }}</h1>
  <div class="meta">大小 {{ size }} · 修改时间 {{ mtime }} · 只读分享</div>
  <a class="btn" href="{{ download_url }}" target="_blank">⬇ 下载文件</a>
  <div class="wrap" style="margin-top:16px;">{{ preview_html | safe }}</div>
</div>
<div class="foot">data.tool · 安全只读分享</div>
</body></html>"""

_ERROR_PAGE = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>{{ title }}</title>
<style>body{font-family:'Microsoft YaHei',sans-serif;background:#f5f6fa;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;color:#333}
.box{text-align:center;background:#fff;padding:40px 60px;border-radius:14px;box-shadow:0 4px 20px rgba(0,0,0,.06)}
.box h1{font-size:20px;color:#FF6B6B}.box p{color:#888;font-size:13px}</style></head>
<body><div class="box"><h1>{{ title }}</h1><p>{{ msg }}</p></div></body></html>"""
