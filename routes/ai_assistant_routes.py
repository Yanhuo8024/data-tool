#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai_assistant_routes.py - AI 对话助手 API 路由
"""

from core_config import exc_response
from flask import Blueprint, request, jsonify, Response
from auth import local_guard
from core_config import OUT_DIR
from ai_assistant import (
    process_chat_message, get_chat_history, clear_chat_history, delete_chat_message,
    record_chat_exchange, strip_thinking,
    get_chat_conversations, create_chat_conversation, delete_chat_conversation,
)
from report_helpers import (
    build_autonomous_report_html, build_analysis_html,
    build_correlation_html, build_group_html,
)
from business_agent import enrich_outbound
import json
import html

ai_assistant_bp = Blueprint('ai_assistant', __name__)
# 高危面（对话消耗 LLM 额度、run_code 执行代码）：远程请求必须认证
ai_assistant_bp.before_request(local_guard)

# 允许的 LLM 提供商白名单（防止任意 provider 注入）
# 仅免费模型：日日新 sensenova（免费公测）/ ollama（本地零成本）/ groq（完全免费）/ zhipu（GLM-4 Flash 免费额度）/ custom
_ALLOWED_PROVIDERS = {'ollama', 'groq', 'zhipu', 'sensenova', 'dashscope', 'custom'}


def _is_safe_custom_url(url: str) -> bool:
    """custom provider 的 apiUrl 安全校验：仅允许公网 http(s)，
    禁止内网/回环/链路本地地址段（防 SSRF 探测内网）。"""
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            return False
        host = parsed.hostname.lower()
        import re as _re
        if host in ('localhost',) or host.endswith('.local') or host.endswith('.internal'):
            return False
        if _re.match(r'^::1$|^fe80:|^fc|^fd', host):  # IPv6 回环/链路本地/ULA
            return False
        if _re.match(r'^127\.', host) or _re.match(r'^10\.', host) \
                or _re.match(r'^192\.168\.', host) or host == '0.0.0.0':
            return False
        m = _re.match(r'^172\.(\d+)\.', host)
        if m and 16 <= int(m.group(1)) <= 31:
            return False
        return True
    except Exception:
        return False


def _sanitize_llm_config(raw_config):
    """服务端校验/清洗前端传入的 LLM 配置：
    - provider 必须在白名单内，否则回退 sensenova（默认免费 provider，2026-09-17 起）
    - apiUrl 仅 custom / ollama 允许自定义，其余使用服务端默认端点
    - custom 的 apiUrl 必须是公网 http(s)（防 SSRF）；ollama 豁免（本机服务属预期）
    - apiKey 交由 llm_client 解析（环境变量优先，前端传入仅兜底），此处不做透传
    """
    if not raw_config or not isinstance(raw_config, dict):
        return None
    cfg = dict(raw_config)
    provider = str(cfg.get('provider', 'sensenova')).lower()
    if provider not in _ALLOWED_PROVIDERS:
        cfg['provider'] = 'sensenova'
        provider = 'sensenova'
    if provider not in ('custom', 'ollama'):
        cfg.pop('apiUrl', None)  # 云端 provider 固定用官方端点，禁止自定义 URL
    elif provider == 'custom':
        # custom = 任意公网兼容端点；内网/回环地址只允许走 ollama（受信任的本机服务）
        if not _is_safe_custom_url(str(cfg.get('apiUrl', ''))):
            raise ValueError("custom apiUrl 必须是公网 http(s) 地址（本机服务请使用 provider=ollama）")
    return cfg


# 多Agent管线可分段指定的阶段名（与 agent_pipeline.STAGE_DEFS + 终稿合成对齐）
_PIPELINE_STAGE_NAMES = ('市场分析师', '量化建模师', '风控合规官', '终稿合成')


def _sanitize_pipeline_models(raw):
    """校验/清洗多Agent分阶段模型配置。

    入参形如 {阶段名: {provider, model}}；仅保留：
    - 阶段名在 _PIPELINE_STAGE_NAMES 内
    - provider 在 _ALLOWED_PROVIDERS 内（非法回退 sensenova）
    - model 非空
    返回清洗后的 dict；非法入参/为空返回 None（后端沿用全局 llm_config，零回归）。
    """
    if not raw or not isinstance(raw, dict):
        return None
    out = {}
    try:
        for name, cfg in raw.items():
            if name not in _PIPELINE_STAGE_NAMES or not isinstance(cfg, dict):
                continue
            prov = str(cfg.get('provider') or 'sensenova').lower()
            if prov not in _ALLOWED_PROVIDERS:
                prov = 'sensenova'
            # 注意：必须显式判 None。str(None) == 'None' 会被当成合法模型名发往
            # provider 造成 400，而不是优雅回退全局模型（JSON null 常见于此）。
            raw_model = cfg.get('model')
            model = str(raw_model).strip() if raw_model is not None else ''
            if not model:
                continue
            # 非字符串标量（int/float/bool）同样不可作为模型名
            if not isinstance(raw_model, str):
                continue
            entry = {'provider': prov, 'model': model}
            # 云端 provider 固定官方端点，禁止前端自定义 url
            if prov not in ('custom', 'ollama'):
                entry.pop('apiUrl', None)
            out[name] = entry
    except Exception:
        return None
    return out or None


def _resolve_pipeline_models(data: dict, llm_config: dict):
    """决定多Agent各阶段使用的模型配置。

    - `pipeline_auto=True`（前端勾选"自动选择大模型"）→ 由后端探测可用 provider 并轮换分配，
      **优先于**任何显式 `pipeline_models`（自动模式下前端不展示手动选项，不存在冲突）；
      探测不到可用 provider 时（如全部无凭据）回退显式配置 / 全局模型。
    - 否则沿用前端显式 `pipeline_models`（经白名单清洗）。
    """
    explicit = _sanitize_pipeline_models(data.get('pipeline_models'))
    if not data.get('pipeline_auto'):
        return explicit
    try:
        from agent_pipeline import resolve_auto_pipeline_models
        auto = resolve_auto_pipeline_models(llm_config)
        if auto:
            return auto
    except Exception as e:
        from core_config import logger
        logger.warning("[AI-PIPE] 自动选择大模型失败，回退显式配置: %s", e)
    return explicit


@ai_assistant_bp.route('/api/assistant/pipeline-auto-models', methods=['GET'])
def api_pipeline_auto_models():
    """预览「自动选择大模型」的结果（零额度：仅凭据探测 + 轮换分配，不发网络请求）。

    供前端在勾选自动模式时展示"当前会用到哪些大模型"，让用户无需手动逐阶段配置。
    """
    try:
        from agent_pipeline import (resolve_auto_pipeline_models, PIPELINE_STAGE_NAMES,
                                    _STAGE_MODEL_PREFS)
        from llm_client import list_available_providers
        _q = request.args
        base_cfg = None
        if _q.get('provider'):
            base_cfg = {'provider': _q.get('provider'), 'model': _q.get('model', '')}
        auto = resolve_auto_pipeline_models(base_cfg) or {}
        avail = list_available_providers()
        # 钉扎标记：哪些阶段当前按用户指定固定了模型（前端展示 📌 图例）
        pinned = {name: True for name, pref in _STAGE_MODEL_PREFS.items()
                  if auto.get(name) == pref}
        return jsonify({
            'ok': True,
            'stages': auto,
            'stage_order': list(PIPELINE_STAGE_NAMES),
            'pinned': pinned,
            'available': [{'provider': p['provider'], 'model': p['model'], 'source': p['source']}
                          for p in avail],
            'note': (f'已自动分配 {len(avail)} 个可用大模型（三阶段并行执行，各自独立模型）'
                     if avail else '未检测到有凭据的云端大模型，将沿用全局模型。'),
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@ai_assistant_bp.route('/api/assistant/chat', methods=['POST'])
def api_assistant_chat():
    """AI 对话接口（stream=true 时返回 SSE 流式输出）"""
    data = request.get_json()
    if not data or 'message' not in data:
        return jsonify({'ok': False, 'error': '缺少 message 参数'}), 400
    
    message = data['message'].strip()
    if not message:
        return jsonify({'ok': False, 'error': '消息不能为空'}), 400
    
    llm_config = _sanitize_llm_config(data.get('llm_config', None))
    current_file = data.get('file') or data.get('current_file')
    mode = data.get('mode', 'think')
    images = data.get('images') or []
    session_id = data.get('session_id')  # 前端生成，用于"停止生成"联动后端取消
    # 多会话：前端持有的会话键。缺省 None → 沿用当前会话（向后兼容旧前端）
    conversation_id = data.get('conversation_id')
    pipeline_models = _resolve_pipeline_models(data, llm_config)

    # 流式输出（SSE）：边生成边显示
    if data.get('stream'):
        from ai_assistant import stream_chat_message
        def generate():
            last_done = None
            try:
                for evt in stream_chat_message(message, llm_config, current_file=current_file,
                                               mode=mode, images=images, session_id=session_id,
                                               pipeline_models=pipeline_models,
                                               conversation_id=conversation_id):
                    payload = evt.get('data', evt)
                    if evt['type'] == 'done':
                        # 出口统一后处理：剥离思维链标签 + 风控初审（见 business_agent）
                        payload = last_done = enrich_outbound(payload, message, current_file)
                    yield f"event: {evt['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"event: done\ndata: {json.dumps({'ok': False, 'error': f'处理失败: {e}'}, ensure_ascii=False)}\n\n"
            finally:
                # 数据闭环：每 N 轮异步触发 LLM 语义摘要（零感知，失败静默）
                try:
                    if last_done and last_done.get('ok') and last_done.get('reply'):
                        from conversation_memory import get_memory
                        get_memory().maybe_update_summary_llm(message, last_done['reply'], llm_config)
                except Exception:
                    pass
                # P0：本轮问答写入会话历史。不写则 data/chat_history.json 恒为空，
                # 前端切标签页或刷新后 loadChatHistory 无内容可恢复，整段会话消失。
                try:
                    if last_done and last_done.get('ok') and last_done.get('reply'):
                        record_chat_exchange(message, last_done['reply'],
                                            conversation_id=conversation_id)
                except Exception:
                    pass
        return Response(generate(), mimetype='text/event-stream',
                        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})
    
    try:
        result = process_chat_message(message, llm_config, current_file=current_file, mode=mode,
                                      images=images, conversation_id=conversation_id)
        # 同步出口与 SSE 同一处后处理：剥离思维链标签 + 风控初审
        result = enrich_outbound(result, message, current_file)
        # P0：写入会话历史（与流式路径一致），保证切标签页 / 刷新后仍可恢复
        try:
            _reply_text = result.get('reply') if isinstance(result, dict) else None
            if _reply_text:
                record_chat_exchange(message, _reply_text, conversation_id=conversation_id)
        except Exception:
            pass
        return jsonify({'ok': True, **result})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'处理失败: {str(e)}'}), 500


@ai_assistant_bp.route('/api/assistant/run_code', methods=['POST'])
def api_assistant_run_code():
    """代码执行接口"""
    data = request.get_json()
    if not data or 'code' not in data:
        return jsonify({'ok': False, 'error': '缺少 code 参数'}), 400

    code = data['code']
    language = data.get('language', 'python')
    context = data.get('context', {})

    # P0-2 安全：Node 子进程与主进程同权限、无真实隔离，JS 执行仅限本机回环访问
    if str(language).lower() in ('javascript', 'js'):
        if request.remote_addr not in ('127.0.0.1', '::1', 'localhost'):
            return jsonify({'ok': False,
                            'error': 'JS 代码执行仅限本机使用（远程请改用 Python，其有 AST 白名单审查）'}), 403

    try:
        from sandbox_exec import execute_code
        result = execute_code(code, language, context)
        return jsonify(result)
    except Exception as e:
        return jsonify({'ok': False, 'error': f'执行失败: {str(e)}'}), 500


@ai_assistant_bp.route('/api/assistant/history', methods=['GET'])
def api_assistant_history():
    """获取聊天历史。

    带 ?conversation_id=xxx → 只返回该会话（老会话自动回退读本地归档，不会被
    主存 100 条上限截断）；不带 → 返回全部（向后兼容旧前端）。
    """
    cid = request.args.get('conversation_id')
    return jsonify({'ok': True, 'data': get_chat_history(cid), 'conversation_id': cid or None})


@ai_assistant_bp.route('/api/assistant/conversations', methods=['GET'])
def api_assistant_conversations():
    """会话列表（最近活动在前）。以本地归档聚合为准，故不受主存 100 条上限影响。"""
    try:
        return jsonify({'ok': True, 'conversations': get_chat_conversations()})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'获取会话列表失败: {e}', 'conversations': []}), 500


@ai_assistant_bp.route('/api/assistant/new-conversation', methods=['POST'])
def api_assistant_new_conversation():
    """新建对话：后端生成合法会话键并切为当前会话，返回该会话摘要。"""
    try:
        conv = create_chat_conversation()
        return jsonify({'ok': True, 'conversation': conv, 'conversation_id': conv['id']})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'新建对话失败: {e}'}), 500


@ai_assistant_bp.route('/api/assistant/delete-conversation', methods=['POST'])
def api_assistant_delete_conversation():
    """删除整个会话（主存消息 + 本地归档一起清），不影响其他会话。"""
    data = request.get_json(silent=True) or {}
    cid = data.get('conversation_id')
    if not cid:
        return jsonify({'ok': False, 'error': '缺少 conversation_id'}), 400
    try:
        removed = delete_chat_conversation(cid)
        return jsonify({'ok': True, 'removed': removed,
                        'message': '已删除会话' if removed else '已删除会话（主存无记录，已清理归档）'})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'删除会话失败: {e}'}), 500


@ai_assistant_bp.route('/api/assistant/conclusions', methods=['GET'])
def api_assistant_conclusions():
    """获取分析结论"""
    from ai_assistant import get_conclusions
    return jsonify({'ok': True, 'conclusions': get_conclusions()})


@ai_assistant_bp.route('/api/assistant/clear', methods=['POST'])
def api_assistant_clear():
    """清空聊天历史。

    带 conversation_id → 只清该会话（🗑 的语义：清空当前对话，不动其它会话）；
    不带 → 全量清空（向后兼容旧前端）。
    """
    data = request.get_json(silent=True) or {}
    cid = data.get('conversation_id')
    clear_chat_history(cid)
    # 同步清空记忆（意图偏好），保持会话一致
    try:
        from conversation_memory import get_memory
        if cid:
            # 只清该会话的短期记忆桶；长期偏好与其它会话保持不动
            get_memory().forget_conversation(cid)
        else:
            get_memory().reset()
    except Exception:
        pass
    return jsonify({'ok': True, 'message': '已清空当前对话' if cid else '已清空'})


@ai_assistant_bp.route('/api/assistant/delete-message', methods=['POST'])
def api_assistant_delete_message():
    """删除单条聊天记录（按内容匹配，可选 timestamp/role 限定）"""
    data = request.get_json(silent=True) or {}
    content = data.get('content')
    if content is None:
        return jsonify({'ok': False, 'error': '缺少 content'}), 400
    removed = delete_chat_message(
        content=content,
        timestamp=data.get('timestamp'),
        role=data.get('role'),
    )
    return jsonify({'ok': True, 'removed': removed, 'message': '已删除' if removed else '未找到匹配记录'})


@ai_assistant_bp.route('/api/assistant/feedback', methods=['POST'])
def api_assistant_feedback():
    """反馈闭环（7.3）：👍/👎 记录为 Bad Case，用于 Prompt 迭代 / 知识库补充 / 同义词扩充"""
    data = request.get_json() or {}
    fb = str(data.get('feedback', '')).lower()
    if fb not in ('like', 'dislike', 'regenerate', 'inaccurate'):
        return jsonify({'ok': False, 'error': 'feedback 取值须为 like/dislike/regenerate/inaccurate'}), 400
    message = (data.get('message') or '')
    session_id = (data.get('session_id') or 'default')
    reply = (data.get('reply') or '')

    recorded = False
    try:
        from feedback_loop import BadCaseCollector
        if fb in ('dislike', 'regenerate', 'inaccurate'):
            collector = BadCaseCollector()
            collector.record(
                user_query=message,
                prompt="",
                sql="",
                error_type="user_feedback",
                error_message=f"用户反馈: {fb}",
                feedback_type=fb,
                session_id=session_id,
            )
            recorded = True
    except Exception as e:
        from core_config import logger
        logger.warning("反馈记录失败: %s", e)

    # 负面反馈触发同义词扩充：把用户口语词记入候选（附录到 intent_router.FINANCIAL_SYNONYMS 提示）
    suggestion = None
    if fb in ('dislike', 'inaccurate') and message:
        suggestion = ("已记录。建议：1) 若回答没命中，说明该词可加入同义词表或知识库；"
                      "2) 若分析不对，会在 Bad Case 中沉淀供 Prompt 迭代。")
    return jsonify({'ok': True, 'recorded': recorded, 'feedback': fb, 'suggestion': suggestion})


@ai_assistant_bp.route('/api/assistant/autonomous-analysis', methods=['POST'])
def api_autonomous_analysis():
    """AI Agent 分析：ReAct 循环（LLM 生成计划 -> 工具调用 -> 喂回 LLM）"""
    data = request.get_json() or {}
    file_name = (data.get('file') or '').strip()
    llm_config = _sanitize_llm_config(data.get('llm_config', None))
    user_message = data.get('message', '')

    # 诊断日志：只记录 provider/model，绝不记录 apiKey/apiUrl（防密钥泄露）
    from core_config import logger
    _prov = (llm_config or {}).get('provider', 'unknown')
    _model = (llm_config or {}).get('model', 'default')
    logger.info(f"[AI-DEBUG] file={file_name!r} message={user_message!r} provider={_prov} model={_model}")

    if not file_name:
        # 纯金融/行情/估值类问题不需要数据文件：允许空文件进入 agent（使用金融工具）
        _fin_kw = ('股票', '估值', '股价', '财报', '行情', 'DCF', '港股', 'A股', 'PE', 'ROE',
                   '腾讯', '茅台', '五粮液', '阿里巴巴', '市盈率', '市净率', '买入', '值得')
        if not any(k in user_message for k in _fin_kw):
            return jsonify({'ok': False, 'error': '缺少 file 参数'}), 400
        file_name = ''
    
    try:
        from ai_agent import agent_loop, get_tracker
        
        # Generate session id for progress tracking
        import uuid
        session_id = data.get('session_id', str(uuid.uuid4())[:8])
        resume = bool(data.get('resume', False))
        
        result = agent_loop(
            user_message=user_message,
            llm_config=llm_config,
            file_name=file_name,
            max_iterations='auto',
            session_id=session_id,
            resume=resume
        )
        
        # Include progress info in response
        tracker = get_tracker(session_id)
        
        # 透传 agent 的 ok/error 状态（含 plan/trace 供前端渲染执行轨迹与恢复入口）
        return jsonify({
            'ok': result.get('ok', True),
            'report': result.get('output', ''),
            'calls': result.get('calls', []),
            'iterations': result.get('iterations', 0),
            'error': result.get('error', ''),
            'cancelled': result.get('cancelled', False),
            'resumable': result.get('resumable', False),
            'plan': result.get('plan'),
            'trace': result.get('trace'),
            'session_id': session_id,
            'progress': tracker.to_dict() if tracker else None
        })
        
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Agent error: {str(e)}'}), 500


@ai_assistant_bp.route('/api/assistant/cancel/<session_id>', methods=['POST'])
def api_cancel(session_id):
    """取消 Agent 执行"""
    from ai_agent import cancel_session
    
    cancel_session(session_id)
    return jsonify({'ok': True, 'message': '已取消'})


@ai_assistant_bp.route('/api/assistant/save-to-report', methods=['POST'])
def api_save_to_report():
    """保存 AI 分析结果到报告"""
    data = request.get_json()
    if not data or 'report' not in data:
        return jsonify({'ok': False, 'error': '缺少 report 参数'}), 400
    
    try:
        report_text = data['report']
        file_name = data.get('file', 'analysis')
        title = data.get('title', 'AI 深度分析报告')
        
        # 生成报告 HTML（report_text 转义防 XSS）
        from datetime import datetime
        safe_title = html.escape(title)
        safe_report = html.escape(report_text)
        html_parts = []
        html_parts.append(f'<h1 style="color:#333;border-bottom:2px solid #FF6B6B;padding-bottom:12px;">{safe_title}</h1>')
        html_parts.append(f'<p style="color:#888;font-size:14px;">生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>')
        html_parts.append(f'<p style="color:#555;font-size:13px;">数据来源: {html.escape(file_name)}</p>')
        html_parts.append('<hr style="border:none;border-top:1px solid #eee;margin:16px 0;">')
        html_parts.append(f'<div style="font-size:14px;line-height:1.8;white-space:pre-wrap;">{safe_report}</div>')
        
        html_content = '\n'.join(html_parts)
        
        # 保存到 output 目录
        from pathlib import Path
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = Path(OUT_DIR) / f"ai_report_{timestamp}.html"
        report_path.write_text(html_content, encoding='utf-8')
        
        return jsonify({
            'ok': True,
            'file': report_path.name,
            'path': str(report_path)
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': f'保存失败: {str(e)}'}), 500


@ai_assistant_bp.route('/api/assistant/reports', methods=['GET'])
def api_assistant_reports():
    """获取已生成的 AI 分析报告列表（聊天历史见 /api/assistant/history）"""
    try:
        from pathlib import Path
        import glob
        
        # 查找所有 AI 报告
        reports = glob.glob(str(Path(OUT_DIR) / "ai_report_*.html"))
        history = []
        for r in sorted(reports, reverse=True)[:20]:
            p = Path(r)
            history.append({
                'file': p.name,
                'time': p.stat().st_mtime,
                'size': p.stat().st_size
            })
        
        return jsonify({'ok': True, 'history': history})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/structured-report', methods=['POST'])
def api_structured_report():
    """生成结构化报告"""
    data = request.get_json()
    if not data or 'file' not in data:
        return jsonify({'ok': False, 'error': '缺少 file 参数'}), 400
    
    file_name = data['file'].strip()
    report_type = data.get('type', 'analyze')
    chart_type = data.get('chart_type', '')
    
    if not file_name:
        return jsonify({'ok': False, 'error': '文件名为空'}), 400
    
    try:
        from core_config import load_dataframe, OUT_DIR
        from ai_analysis_engine import EnhancedAnalysisEngine
        import pandas as pd

        # P0-1 路径守卫：显式拒绝 output/ 之外的文件（400 而非笼统 500）
        df = load_dataframe(str(OUT_DIR / file_name))
        if df is None or df.empty:
            return jsonify({'ok': False, 'error': '无法读取数据文件'}), 400
        
        engine = EnhancedAnalysisEngine(df, file_name)
        
        html = ''
        if report_type == 'autonomous':
            report = engine.full_analysis('')
            html = build_autonomous_report_html(report, file_name)
        elif report_type == 'analyze':
            report = engine.full_analysis('')
            html = build_analysis_html(report, file_name)
        elif report_type == 'correlation':
            corr = engine._correlation_analysis()
            html = build_correlation_html(corr, file_name)
        elif report_type == 'group':
            segments = engine._segment_analysis()
            html = build_group_html(segments, file_name)
        elif report_type == 'clean':
            html = f'<div style="padding:16px;"><h4>Data cleaning completed</h4><p>File: {html.escape(file_name)}</p></div>'
        elif report_type == 'chart':
            html = f'<div style="padding:16px;"><h4>Chart generated</h4><p>File: {html.escape(file_name)}</p></div>'
        else:
            report = engine.full_analysis('')
            html = build_autonomous_report_html(report, file_name)
        
        return jsonify({'ok': True, 'html': html})
    except PermissionError as e:
        # P0-1：路径守卫拒绝 → 400 明确提示
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': f'Report generation failed: {str(e)}'}), 500


# ----------------------------------------------------------------------
# P0: Agent 结构化追踪 API（JSONL 轨迹 -> 前端时间线 / 坏 Case 回放）
# ----------------------------------------------------------------------
@ai_assistant_bp.route('/api/assistant/traces', methods=['GET'])
def api_trace_list():
    """列出最近的 Agent 运行轨迹（坏 Case 可筛选 ok=false）"""
    try:
        from trace_store import list_runs
        limit = request.args.get('limit', default=30, type=int)
        ok = request.args.get('ok')  # true/false 筛选
        runs = list_runs(limit=min(limit, 200))
        if ok is not None:
            want = ok.lower() == 'true'
            runs = [r for r in runs if ('ok' in r) and r['ok'] is want]
        return jsonify({'ok': True, 'data': runs})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/traces/<run_id>', methods=['GET'])
def api_trace_get(run_id):
    """获取单个运行轨迹的完整事件流（回放坏 Case）"""
    try:
        from trace_store import read_run
        run = read_run(run_id)
        if run is None:
            return jsonify({'ok': False, 'error': f'轨迹不存在: {run_id}'}), 404
        return jsonify({'ok': True, 'run': run['run_id'], 'count': run['count'],
                        'file': run['file'], 'events': run['events']})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/traces/<run_id>', methods=['DELETE'])
def api_trace_delete(run_id):
    """删除单个运行轨迹"""
    try:
        from trace_store import delete_run, read_run
        if read_run(run_id) is None:
            return jsonify({'ok': False, 'error': f'轨迹不存在: {run_id}'}), 404
        delete_run(run_id)
        return jsonify({'ok': True, 'message': '已删除'})
    except Exception as e:
        return exc_response(e)


# ----------------------------------------------------------------------
# Agent 流程可视化 API（归一化 + 故障定位）
#
# 为什么放在本蓝图：ai_assistant 属于 routes/__init__ 的 _CORE_REGISTRARS，
# 随 import 同步注册，服务起来即可用；而 agent_routes 等懒注册蓝图在首批请求
# 到达时可能尚未注册完成（Flask 收到首个请求后拒绝 register_blueprint），
# 会出现"启动后永久 404"。可观测性接口自己不能不可观测，故放这里。
#
# 静态路径（/flows/health、/flows/rules）必须先于 /flows/<run_id> 声明，
# 避免被变量转换器抢走。
# ----------------------------------------------------------------------
@ai_assistant_bp.route('/api/assistant/flows', methods=['GET'])
def api_flows_list():
    """最近若干次 Agent 运行的流程概览（含健康态与首要故障）→ 可视化列表页数据源。"""
    try:
        import agent_flow
        limit = request.args.get('limit', default=30, type=int)
        return jsonify({'ok': True, 'data': agent_flow.summarize_recent(limit)})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flows/health', methods=['GET'])
def api_flows_health():
    """跨运行聚合：各故障码 / 各阶段出现频率排行 → 回答"哪一部分最常出问题"。"""
    try:
        import agent_flow
        limit = request.args.get('limit', default=100, type=int)
        return jsonify({'ok': True, 'data': agent_flow.aggregate_health(limit)})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flows/problems', methods=['GET'])
def api_flows_problems():
    """Agent 问题页数据源：时间分位统计 + 问题台账 + 优化方向。

    页面：static/agent-problems.html
    查询参数：limit（默认 100，最大 200）
    """
    try:
        import agent_flow
        limit = request.args.get('limit', default=100, type=int)
        return jsonify({'ok': True, 'data': agent_flow.problems_dashboard(limit)})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flows/rules', methods=['GET'])
def api_flows_rules():
    """诊断规则清单（严重度 + 说明），供前端图例与文档使用。零额度。"""
    try:
        import agent_flow
        return jsonify({'ok': True, 'data': agent_flow.rule_catalog()})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flows/<run_id>', methods=['GET'])
def api_flow_get(run_id):
    """单次运行的归一化流程图：阶段泳道 + 节点时间线 + 诊断结论。

    查询参数：
      raw=1  附带原始事件流（坏 Case 回放用）
    未收尾的运行同样可读（按文件当前内容给出 running/interrupted 判定），
    因此也能用于追踪"正在跑"的那一次。
    """
    try:
        import agent_flow
        raw = request.args.get('raw', '0') in ('1', 'true', 'yes')
        flow = agent_flow.build_flow(run_id, include_events=raw)
        if flow is None:
            return jsonify({'ok': False, 'error': f'轨迹不存在: {run_id}'}), 404
        return jsonify({'ok': True, 'flow': flow})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flows/<run_id>/diagnose', methods=['GET'])
def api_flow_diagnose(run_id):
    """轻量诊断：只要「哪一部分出问题」的结论，不含节点明细。"""
    try:
        import agent_flow
        d = agent_flow.diagnose_run(run_id)
        if d is None:
            return jsonify({'ok': False, 'error': f'轨迹不存在: {run_id}'}), 404
        return jsonify({'ok': True, **d})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flows/<run_id>/timeline', methods=['GET'])
def api_flow_timeline(run_id):
    """轻量时间线：可逐帧步进的 {seq, type, label, status} 列表（不含 data 大字段）。

    供前端做回放进度条与单步前进/后退。一次运行的 data 可能含完整 prompt，
    故此处**刻意剥离** data，避免响应体过大。
    """
    try:
        import agent_flow
        tl = agent_flow.timeline(run_id)
        if tl is None:
            return jsonify({'ok': False, 'error': f'轨迹不存在: {run_id}'}), 404
        return jsonify({'ok': True, **tl})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flows/<run_id>/replay', methods=['GET'])
def api_flow_replay(run_id):
    """观测回放：把运行重放到第 `seq` 个事件为止，返回**当时**的节点/指标/诊断。

    查询参数：
      seq=12   回放到 seq<=12（闭区间）；缺省或越界自动夹到区间内
      raw=1    附带该前缀的原始事件流

    纯只读、零额度、零副作用（**不**恢复执行）。真恢复执行走 agent_loop(resume=True)。
    """
    try:
        import agent_flow
        raw = request.args.get('raw', '0') in ('1', 'true', 'yes')
        seq = request.args.get('seq')
        if seq is None:
            # 缺省 = 整段回放（等价于 build_flow，但走 replay 语义便于前端统一处理）
            tl = agent_flow.timeline(run_id)
            if tl is None:
                return jsonify({'ok': False, 'error': f'轨迹不存在: {run_id}'}), 404
            seq = tl.get('last_seq', 0)
        flow = agent_flow.replay_from(run_id, seq, include_events=raw)
        if flow is None:
            return jsonify({'ok': False, 'error': f'轨迹不存在: {run_id}'}), 404
        return jsonify({'ok': True, 'flow': flow})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flows/<run_id>/fork', methods=['GET'])
def api_flow_fork(run_id):
    """分叉上下文包：给出从第 `seq` 步分叉所需的可注入摘要（**不**自动重跑）。

    查询参数：seq=12
    返回 injected_context 可直接拼进新一轮 user 消息，让模型继承已知取数结果。
    """
    try:
        import agent_flow
        seq = request.args.get('seq', '0')
        pkg = agent_flow.fork_at(run_id, seq)
        if pkg is None:
            return jsonify({'ok': False, 'error': f'轨迹不存在: {run_id}'}), 404
        return jsonify({'ok': True, 'fork': pkg})
    except Exception as e:
        return exc_response(e)


# ----------------------------------------------------------------------
# Agent 工作流可视化器 API（画布版 · 同源，无需独立桥接服务 8788）
#
# 页面：static/agent-flow-viz.html（由「Agent 流程」标签页以 iframe 承载）
# 数据由 flowviz_adapter 从 trace_store 直读后翻译成 Spec + 状态快照，
# 供画布渲染节点/连线/逐步计时/问题报错三个视图。
#
# 与 /api/assistant/flows* 的分工：
#   flows*   归一化 + 诊断（列表页 / 问题页用，偏「分析」）
#   flowviz* 画布渲染契约（Spec/state，偏「可视化回放」）
# ----------------------------------------------------------------------
_FLOWVIZ_FOLLOW = ('', 'latest', 'last', 'newest', '-')


def _flowviz_follow(run):
    """判断是否处于「跟随最新运行」模式（决定 done 是否置真让画布停轮询）。"""
    return str(run or '').strip().lower() in _FLOWVIZ_FOLLOW


@ai_assistant_bp.route('/api/assistant/flowviz/runs', methods=['GET'])
def api_flowviz_runs():
    """运行清单（新的在前）→ 画布的运行选择器。"""
    try:
        import flowviz_adapter
        limit = request.args.get('limit', default=20, type=int)
        return jsonify({'ok': True, 'runs': flowviz_adapter.list_runs(None, limit)})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flowviz/spec', methods=['GET'])
def api_flowviz_spec():
    """由某次运行生成画布拓扑 Spec（节点 / 连线 / 阶段）。run 省略即最新一次。"""
    try:
        import flowviz_adapter
        run = request.args.get('run') or None
        try:
            trace, used, skipped = flowviz_adapter.fetch_trace_soft(None, run)
        except flowviz_adapter.UpstreamMissing as e:
            return jsonify({'ok': False, 'error': str(e), 'tried': getattr(e, 'tried', [])}), 404
        spec = flowviz_adapter.build_spec(trace)
        return jsonify({'ok': True, 'run': used, 'spec': spec})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flowviz/state', methods=['GET'])
def api_flowviz_state():
    """状态快照（agents / links / summary / logs / timeline / issues / 拓扑变更随附 spec）。

    查询参数：
      run=latest|空  跟随最新一次运行（done 保持 false，画布继续等下一次运行）
      run=<run_id>   钉住某次运行（跑完即 done=true，画布定格）
    """
    try:
        import flowviz_adapter
        run = request.args.get('run') or None
        cid = request.args.get('cid') or ''
        try:
            trace, used, skipped = flowviz_adapter.fetch_trace_soft(None, run)
        except flowviz_adapter.UpstreamMissing as e:
            return jsonify({'ok': False, 'error': str(e), 'tried': getattr(e, 'tried', [])}), 404
        state = flowviz_adapter.build_state(trace, cid=cid,
                                            keepalive=_flowviz_follow(run))
        payload = {'ok': True}
        payload.update(state)
        payload.setdefault('run', used)
        if skipped:
            payload['fell_back_from'] = [str(x[0]) for x in skipped]
        return jsonify(payload)
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/flowviz/raw', methods=['GET'])
def api_flowviz_raw():
    """上游原始事件流（排障用）：确认画布拿到的到底是什么。"""
    try:
        import flowviz_adapter
        run = request.args.get('run') or None
        try:
            trace, used, skipped = flowviz_adapter.fetch_trace_soft(None, run)
        except flowviz_adapter.UpstreamMissing as e:
            return jsonify({'ok': False, 'error': str(e), 'tried': getattr(e, 'tried', [])}), 404
        return jsonify({'ok': True, 'run': used, 'count': trace.get('count'),
                        'file': trace.get('file'), 'events': trace.get('events') or []})
    except Exception as e:
        return exc_response(e)


# ----------------------------------------------------------------------
# P2a: 多 Agent 辩论状态图 API（可持久化 / 暂停 / 分支 / 会话恢复）
# ----------------------------------------------------------------------
@ai_assistant_bp.route('/api/assistant/debate', methods=['GET'])
def api_debate_list():
    """列出最近的辩论会话状态图"""
    try:
        from debate_engine import list_graphs
        return jsonify({'ok': True, 'data': list_graphs(limit=20)})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/debate', methods=['POST'])
def api_debate_create():
    """创建（并可能完整执行）一个辩论状态图。body:
    {message, llm_config?, file?, initial_analysis?, session_id?, parallel_branching?, step?}
    step=true 时仅创建并执行一步（供前端逐步/暂停驱动）；否则一次执行完整辩论。"""
    data = request.get_json() or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'ok': False, 'error': '缺少 message 参数'}), 400
    try:
        llm_config = _sanitize_llm_config(data.get('llm_config', None))
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    file_name = (data.get('file') or '').strip() or None
    initial_analysis = data.get('initial_analysis')
    session_id = data.get('session_id') or 'default'
    branch = bool(data.get('parallel_branching', True))
    step = bool(data.get('step', False))
    try:
        from debate_engine import create_debate_graph, step_graph, collect_result
        g = create_debate_graph(message, llm_config or {'provider': 'sensenova'},
                                file_name=file_name, initial_analysis=initial_analysis,
                                session_id=session_id)
        if step:
            more = step_graph(g, branch=branch)
            # 若恰好一步完成，直接返回结果
            res = collect_result(g)
            return jsonify({'ok': True, 'graph_id': g.graph_id, 'status': g.status,
                            'more': more, 'result': res,
                            'num_nodes': len(g.nodes)})
        # 非 step：完整执行
        while step_graph(g, branch=branch):
            pass
        res = collect_result(g)
        if not res:
            return jsonify({'ok': False, 'error': '辩论执行未完成'}), 500
        res.pop('rounds', None)
        return jsonify({'ok': True, **res})
    except Exception as e:
        return jsonify({'ok': False, 'error': f'辩论失败: {str(e)}'}), 500


@ai_assistant_bp.route('/api/assistant/debate/<graph_id>', methods=['GET'])
def api_debate_get(graph_id):
    """获取辩论状态图详情（含节点时间线）"""
    try:
        from debate_engine import load_graph
        g = load_graph(graph_id)
        if g is None:
            return jsonify({'ok': False, 'error': f'会话不存在: {graph_id}'}), 404
        nodes = []
        for node in g.nodes.values():
            nodes.append({'node_id': node.node_id, 'role': node.role,
                          'parent_ids': node.parent_ids, 'completed': node.completed,
                          'content': node.content[:3000]})
        return jsonify({'ok': True, 'graph_id': g.graph_id, 'status': g.status,
                        'message': g.message, 'nodes': nodes,
                        'usage': g.usage_total})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/debate/<graph_id>/step', methods=['POST'])
def api_debate_step(graph_id):
    """推进辩论状态图一步（暂停/恢复后的继续执行）"""
    data = request.get_json() or {}
    action = data.get('action', 'step')  # step / pause / resume
    branch = bool(data.get('parallel_branching', True))
    try:
        from debate_engine import load_graph, step_graph, pause_graph, resume_graph, collect_result
        g = load_graph(graph_id)
        if g is None:
            return jsonify({'ok': False, 'error': f'会话不存在: {graph_id}'}), 404
        if action == 'pause':
            pause_graph(g)
            return jsonify({'ok': True, 'status': g.status})
        if action == 'resume':
            resume_graph(g)
            return jsonify({'ok': True, 'status': g.status})
        # step
        more = step_graph(g, branch=branch)
        res = collect_result(g)
        return jsonify({'ok': True, 'status': g.status, 'more': more,
                        'result': res, 'num_nodes': len(g.nodes)})
    except Exception as e:
        return exc_response(e)


@ai_assistant_bp.route('/api/assistant/debate/<graph_id>', methods=['DELETE'])
def api_debate_delete(graph_id):
    """删除辩论会话"""
    try:
        from debate_engine import load_graph, delete_graph
        if load_graph(graph_id) is None:
            return jsonify({'ok': False, 'error': f'会话不存在: {graph_id}'}), 404
        delete_graph(graph_id)
        return jsonify({'ok': True, 'message': '已删除'})
    except Exception as e:
        return exc_response(e)
