"""System routes — reports, tasks, pipeline, workspace, monitoring, SQL, snapshots, RAG"""
from core_config import exc_response
import json
import inspect
import traceback
import threading

from flask import request, jsonify, send_file, Blueprint
from pathlib import Path
from datetime import datetime

from auth import require_auth_or_api_key, local_guard

from core_config import (
    OUT_DIR, safe_data_path, pd, os, glob, re, time, logger,
    HAS_REPORT, save_report, list_templates,
    task_queue, pipeline_store, NODE_INFO,
    init_monitoring, memory_monitor, task_tracker, get_system_health, get_recent_errors,
    rag_engine, UPLOAD_DIR,
    _get_akshare, get_ml_trainer,
    prophet_forecast, arima_sarima_model,
    get_kline, backtest_ma_crossover, backtest_macd,
    money_flow_chart, chip_distribution_chart, interactive_heatmap,
    fetch_all_stocks, segment_and_count, generate_wordcloud,
    fetch_financial_data, clean, analyze,
)

# 初始化监控
init_monitoring()


# ============================================================
# 后台清理线程
# ============================================================

def _cleanup_worker():
    """后台定期清理过期任务"""
    while True:
        time.sleep(300)  # 每5分钟
        try:
            task_queue.cleanup_expired(max_age=3600)
        except Exception:
            pass

cleanup_thread = threading.Thread(target=_cleanup_worker, daemon=True)


def register_routes(app, limiter):
    bp = Blueprint('system_routes', __name__)
    # 高危面（任务提交/akshare 任意调用/文件操作）：远程请求必须认证
    bp.before_request(local_guard)
    # ============================================================
    #  Health API（安全中心「系统状态」使用）
    # ============================================================

    @bp.route("/api/monitoring/health", methods=["GET"])
    @limiter.limit("30 per minute")
    def api_health():
        """系统健康状态（内存/CPU/磁盘/运行时长）"""
        try:
            return jsonify({"ok": True, **get_system_health()})
        except Exception as e:
            logger.warning(f"health 接口异常: {e}")
            return exc_response(e)

    # ============================================================
    #  Report API（大部分已移至 file_routes.py，保留预览）
    # ============================================================

    @bp.route("/api/report/preview", methods=["POST"])
    @limiter.limit("10 per minute")
    def api_report_preview():
        """从收集的图表生成报告预览"""
        try:
            data = request.json
            if not data:
                return jsonify({"error": "无效的请求数据"}), 400

            charts = data.get("charts", [])
            conclusions = data.get("conclusions", [])
            template = data.get("template", "none")
            file_name = data.get("file", "")

            if not charts:
                return jsonify({"error": "没有可生成报告的图表"}), 400

            # 清理 charts 数据，移除可能导致 JSON 问题的字符
            for c in charts:
                if c.get("html"):
                    # 移除换行符和控制字符，保留 HTML 标签
                    c["html"] = re.sub(r'[\r\n\t]+', ' ', c["html"])
                    c["html"] = re.sub(r'\s+', ' ', c["html"])
                if c.get("stats"):
                    c["stats"] = str(c["stats"]).replace('\n', ' ').replace('\r', '')

            # 根据模板生成不同风格
            if template == "none":
                title = "数据分析报告"
                subtitle = "图表汇总"
            elif template == "family_care":
                title = "家庭照护需求与消费结构研究报告"
                subtitle = "基于多省份面板数据的实证分析"
            elif template == "financial_analysis":
                title = "金融数据分析报告"
                subtitle = "量化分析与策略回测"
            else:
                title = "数据探索性分析报告 (EDA)"
                subtitle = file_name

            html_parts = []
            html_parts.append(f'<h1 style="color:#333;border-bottom:2px solid #FF6B6B;padding-bottom:12px;">{title}</h1>')
            html_parts.append(f'<p style="color:#888;font-size:14px;">{subtitle} · 生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M")}</p>')
            html_parts.append(f'<p style="color:#555;font-size:13px;">数据来源: {file_name or "未指定"}</p>')
            html_parts.append('<hr style="border:none;border-top:1px solid #eee;margin:16px 0;">')

            html_parts.append('<h2 style="color:#333;">分析结果 ({})</h2>'.format(len(charts)))

            for i, c in enumerate(charts):
                html_parts.append('<div style="margin:20px 0;padding:16px;background:#fafafa;border-radius:8px;border:1px solid #eee;">')
                html_parts.append(f'<h3 style="color:#FF6B6B;margin-bottom:8px;">{i+1}. {c.get("title", "图表")}</h3>')
                html_parts.append(f'<div style="font-size:12px;color:#888;margin-bottom:8px;">来源: {c.get("source", "")} · {c.get("time", "")}</div>')
                if c.get("img"):
                    html_parts.append(f'<img src="/api/chart/charts/{c["img"]}" style="max-width:100%;border-radius:4px;">')
                if c.get("html"):
                    html_parts.append(f'<div style="margin-top:8px;overflow-x:auto;font-size:12px;color:#333;">{c["html"]}</div>')
                if c.get("stats"):
                    html_parts.append(f'<div style="margin-top:8px;font-size:13px;color:#555;background:#fff;padding:8px;border-radius:4px;">{c["stats"]}</div>')
                html_parts.append('</div>')

            html_parts.append('<hr style="border:none;border-top:1px solid #eee;margin:16px 0;">')
            html_parts.append('<p style="color:#888;font-size:12px;text-align:center;">— 报告由 data.tool 自动生成 —</p>')

            # 结论区（AI 助手 + 各分析页同步的结论）
            if conclusions:
                html_parts.append('<h2 style="color:#333;">本次会话结论 ({})</h2>'.format(len(conclusions)))
                for i, c in enumerate(conclusions):
                    html_parts.append('<div style="margin:12px 0;padding:12px 14px;background:#fdf6f6;border-left:3px solid #FF6B6B;border-radius:6px;">')
                    html_parts.append(f'<div style="font-size:12px;color:#888;margin-bottom:4px;">{i+1}. {c.get("title", "结论")} · 来源: {c.get("source", "")} · {c.get("time", "")}</div>')
                    html_parts.append(f'<div style="font-size:13px;color:#444;line-height:1.8;">{str(c.get("content", ""))[:600]}</div>')
                    html_parts.append('</div>')
                html_parts.append('<hr style="border:none;border-top:1px solid #eee;margin:16px 0;">')

            preview_html = "\n".join(html_parts)

            # 保存完整报告文件
            report_name = f"report_{int(datetime.now().timestamp())}.html"
            report_dir = OUT_DIR / "reports"
            report_dir.mkdir(exist_ok=True)
            full_html = f'''<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:900px;margin:20px auto;padding:20px;color:#333;}}img{{max-width:100%;border-radius:4px;margin:12px 0;}}h1{{border-bottom:2px solid #FF6B6B;padding-bottom:12px;}}h2{{color:#333;margin-top:24px;}}h3{{color:#FF6B6B;}}table{{border-collapse:collapse;width:100%;}}th,td{{border:1px solid #ddd;padding:8px;text-align:left;}}th{{background:#f5f5f5;}}</style>
</head><body>{preview_html}</body></html>'''
            with open(str(report_dir / report_name), 'w', encoding='utf-8') as f:
                f.write(full_html)

            # 返回 JSON
            result = {"ok": True, "preview_html": preview_html, "file": report_name}
            return app.response_class(
                json.dumps(result, ensure_ascii=False),
                mimetype='application/json'
            )
        except Exception as e:
            return exc_response(e)

    # ============================================================
    #  Report → Word 导出（用户可自定义保存路径与文件名）
    # ============================================================

    @bp.route("/api/report/export-docx", methods=["POST"])
    @limiter.limit("5 per minute")
    def api_report_export_docx():
        """将本次会话图表+结论导出为 Word 文档（python-docx）"""
        try:
            data = request.json or {}
            charts = data.get("charts", [])
            conclusions = data.get("conclusions", [])
            title = data.get("title", "数据分析报告")
            subtitle = data.get("subtitle", "图表汇总 · 由 data.tool 生成")

            from docx import Document
            from docx.shared import Inches, Pt
            import base64, io

            doc = Document()
            doc.add_heading(title, 0)
            doc.add_paragraph(subtitle)
            doc.add_paragraph(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

            # ---------- 结论区 ----------
            if conclusions:
                doc.add_heading("本次会话结论", level=1)
                for c in conclusions:
                    p = doc.add_paragraph()
                    run = p.add_run(f"{c.get('title', '结论')}  ·  {c.get('source', '')}  ·  {c.get('time', '')}")
                    run.bold = True
                    doc.add_paragraph(str(c.get('content', '')))

            # ---------- 图表区 ----------
            doc.add_heading(f"分析结果 ({len(charts)})", level=1)
            for i, c in enumerate(charts):
                doc.add_heading(f"{i + 1}. {c.get('title', '图表')}", level=2)
                doc.add_paragraph(f"来源: {c.get('source', '')} · {c.get('time', '')}")

                # 图片：img 字段 / html 内嵌 <img>
                img_src = None
                html = c.get('html', '') or ''
                img = c.get('img', '') or ''
                candidates = [img]
                m = re.search(r'<img[^>]+src="([^"]+)"', html)
                if m:
                    candidates.append(m.group(1))
                for src in candidates:
                    if not src:
                        continue
                    if src.startswith('data:image'):
                        mm = re.match(r'data:image/(png|jpeg|jpg|gif);base64,(.+)', src)
                        if mm:
                            try:
                                img_src = io.BytesIO(base64.b64decode(mm.group(2)))
                                break
                            except Exception:
                                img_src = None
                    elif src.startswith('/api/chart/charts/'):
                        p2 = OUT_DIR / 'charts' / os.path.basename(src)
                        if p2.exists():
                            img_src = str(p2)
                            break
                if img_src:
                    try:
                        doc.add_picture(img_src, width=Inches(6.0))
                    except Exception:
                        pass

                if c.get('stats'):
                    doc.add_paragraph(str(c['stats']))

                # HTML 表格 → Word 表格
                if '<table' in html:
                    for t in re.findall(r'<table.*?</table>', html, re.S):
                        rows = re.findall(r'<tr.*?</tr>', t, re.S)
                        if not rows:
                            continue
                        first_cells = re.findall(r'<t[hd].*?</t[hd]>', rows[0], re.S)
                        doc_table = doc.add_table(rows=len(rows), cols=max(len(first_cells), 1))
                        doc_table.style = 'Light Grid Accent 1'
                        for ri, row in enumerate(rows):
                            cells = re.findall(r'<t[hd].*?</t[hd]>', row, re.S)
                            for ci, cell in enumerate(cells):
                                if ci >= len(doc_table.columns):
                                    break
                                txt = re.sub(r'<[^>]+>', ' ', cell)
                                txt = re.sub(r'\s+', ' ', txt).strip()
                                doc_table.cell(ri, ci).text = txt
                    doc.add_paragraph('')
                elif html and not img_src:
                    # 无表格无图：提取纯文本摘要
                    text = re.sub(r'<[^>]+>', ' ', html)
                    text = re.sub(r'\s+', ' ', text).strip()
                    if text:
                        doc.add_paragraph(text[:500])

            doc.add_paragraph('— 报告由 data.tool 自动生成 —')

            buffer = io.BytesIO()
            doc.save(buffer)
            buffer.seek(0)
            from flask import Response
            return Response(
                buffer.getvalue(),
                mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                headers={
                    'Content-Disposition': 'attachment; filename=data_tool_report.docx',
                    'Content-Length': str(len(buffer.getvalue())),
                }
            )
        except Exception as e:
            return exc_response(e)

    # ============================================================
    #  Task Queue API (异步任务队列)
    # ============================================================

    @bp.route("/api/task/submit", methods=["POST"])
    def api_task_submit():
        """提交异步任务"""
        try:
            data = request.json
            task_name = data.get("task_name", "")
            params = data.get("params", {})

            # 各任务执行函数（通过闭包捕获 params）
            def run_prophet():
                file_path = safe_data_path(params["file"])
                df = pd.read_csv(file_path)
                return prophet_forecast(df, params.get("date_col", "date"),
                                       params.get("value_col", "value"),
                                       params.get("periods", 30))

            def run_kline():
                return get_kline(
                    symbol=params["symbol"],
                    period=params.get("period", "daily"),
                    start=params.get("start", ""),
                    end=params.get("end", ""),
                    adjust=params.get("adjust", "qfq"),
                )

            def run_arima():
                file_path = safe_data_path(params["file"])
                df = pd.read_csv(file_path)
                return arima_sarima_model(df, params.get("date_col", "date"),
                                         params.get("value_col", "value"),
                                         params.get("order", [1,1,1]),
                                         params.get("steps", 30))

            def run_backtest():
                file_path = safe_data_path(params["file"])
                df = pd.read_csv(file_path)
                strategy = params.get("strategy", "ma_crossover")
                col = params.get("col", "close")
                benchmark = params.get("benchmark", "000300")
                if strategy == "ma_crossover":
                    return backtest_ma_crossover(df, col, benchmark=benchmark)
                else:
                    return backtest_macd(df, col, benchmark=benchmark)

            def run_ml_train():
                file_path = safe_data_path(params.get("file", ""))
                if not file_path:
                    csv_files = sorted(glob.glob(str(OUT_DIR / "*.csv")), key=os.path.getmtime, reverse=True)
                    if not csv_files:
                        raise FileNotFoundError("No CSV files found")
                    file_path = csv_files[0]
                df = pd.read_csv(file_path)
                _Trainer = get_ml_trainer()
                if _Trainer is None:
                    raise RuntimeError("ML 模块不可用（ml_module 未加载）")
                trainer = _Trainer(df)
                task_type = params.get("task_type", "regression")
                if task_type == "regression":
                    result = trainer.train_regression(**params.get("train_params", {}))
                elif task_type == "classification":
                    result = trainer.train_classification(**params.get("train_params", {}))
                else:
                    result = trainer.train_clustering(**params.get("train_params", {}))
                # Save model
                model_path = os.path.join(OUT_DIR, f"ml_model_{int(time.time())}.pkl")
                import pickle
                with open(model_path, 'wb') as f:
                    pickle.dump(trainer, f)
                result['model_path'] = model_path
                return result

            def run_advanced_viz():
                file_path = safe_data_path(params.get("file", ""))
                if not file_path.exists():
                    raise FileNotFoundError(f"{file_path} not found")
                df = pd.read_csv(file_path)
                col = params.get("col", "close")
                charts = {}
                charts['money_flow'] = money_flow_chart(df, col)
                charts['chip'] = chip_distribution_chart(df, col)
                charts['heatmap'] = interactive_heatmap(df, col)
                return charts

            def run_screener():
                return fetch_all_stocks()

            def run_nlp():
                file_path = safe_data_path(params["file"])
                df = pd.read_csv(file_path)
                col = params.get("col", df.columns[0])
                return segment_and_count(df, col)

            def run_fs():
                code = params.get("code", "")
                return fetch_financial_data(code)

            def run_wordcloud():
                file_path = safe_data_path(params["file"])
                df = pd.read_csv(file_path)
                col = params.get("col", df.columns[0])
                return generate_wordcloud(df, col)

            def run_analyze():
                file_path = safe_data_path(params.get("file", ""))
                if not file_path:
                    raise FileNotFoundError("No file specified")
                df = pd.read_csv(file_path)
                return analyze(df, group_col=params.get("group_col"), charts=True)

            def run_clean():
                file_path = safe_data_path(params.get("file", ""))
                df = pd.read_csv(file_path)
                return clean(df, **params.get("clean_params", {}))

            def run_akshare():
                func_name = params.get("func", "stock_zh_a_spot_em")
                func_params = params.get("func_params", {})
                # 白名单约束：仅允许 AK_DATA_FUNCS 中声明的函数（防任意函数调用）
                from core_config import AK_DATA_FUNCS
                if func_name not in AK_DATA_FUNCS:
                    raise ValueError(
                        f"akshare 函数不在白名单内: {func_name}。"
                        f"允许: {', '.join(sorted(AK_DATA_FUNCS.keys()))}")
                ak = _get_akshare()
                if not ak:
                    raise ImportError("akshare not installed")
                func = getattr(ak, func_name, None)
                if func is None:
                    raise ValueError(f"akshare 无此函数: {func_name}")
                result = func(**func_params)
                return result

            # 任务映射
            task_map = {
                "prophet": (run_prophet, "Prophet预测"),
                "arima": (run_arima, "ARIMA建模"),
                "backtest": (run_backtest, "策略回测"),
                "ml_train": (run_ml_train, "ML模型训练"),
                "advanced_viz": (run_advanced_viz, "高级图表"),
                "screener": (run_screener, "股票筛选"),
                "nlp": (run_nlp, "NLP分析"),
                "wordcloud": (run_wordcloud, "词云生成"),
                "fs": (run_fs, "财务报表"),
                "analyze": (run_analyze, "数据分析"),
                "clean": (run_clean, "数据清洗"),
                "akshare": (run_akshare, "akshare数据获取"),
                "kline": (run_kline, "K线数据获取（含复权）"),
            }

            if task_name not in task_map:
                return jsonify({"error": f"Unknown task: {task_name}", "available": list(task_map.keys())}), 400

            fn, label = task_map[task_name]

            # 包装函数，自动注入 progress 和 task_log
            sig = inspect.signature(fn)
            kwargs = {}
            if 'progress' in sig.parameters:
                task_queue_ref = task_queue  # capture reference
                def _progress(pct, msg=""):
                    # 当前任务ID在 submit 后才知道，通过全局查找
                    pass
                kwargs['progress'] = _progress
            if 'task_log' in sig.parameters:
                def _log(msg):
                    pass
                kwargs['task_log'] = _log

            task_id = task_queue.submit(label, fn, **kwargs)

            return jsonify({"task_id": task_id, "status": "pending", "name": label})

        except Exception as e:
            return exc_response(e)


    @bp.route("/api/task/status", methods=["GET"])
    def api_task_status():
        """查询任务状态"""
        task_id = request.args.get("task_id")
        info = task_queue.get_task(task_id)
        if not info:
            return jsonify({"error": "task not found"}), 404
        return jsonify(info.to_dict())


    @bp.route("/api/task/cancel", methods=["POST"])
    def api_task_cancel():
        """取消任务"""
        body = request.get_json(silent=True) or {}
        task_id = body.get("task_id")
        if not task_id:
            return jsonify({"error": "缺少参数：task_id"}), 400
        if task_queue.cancel(task_id):
            return jsonify({"ok": True, "status": "cancelled"})
        return jsonify({"error": "cannot cancel task (may be running or not found)"}), 400


    @bp.route("/api/task/stream")
    def api_task_stream():
        """SSE 实时获取任务进度"""
        task_id = request.args.get("task_id")
        return task_queue.sse_response(task_id)


    @bp.route("/api/task/list", methods=["GET"])
    def api_task_list():
        """列出最近的任务"""
        limit = request.args.get("limit", 20, type=int)
        return jsonify(task_queue.list_tasks(limit))


    # ============================================================
    #  Pipeline API (DAG)
    # ============================================================

    @bp.route("/api/pipeline/node/info", methods=["GET"])
    def api_pipeline_node_info():
        """返回所有节点类型信息"""
        return jsonify(NODE_INFO)


    @bp.route("/api/pipeline/create", methods=["POST"])
    def api_pipeline_create():
        """创建 Pipeline"""
        d = request.json
        p = pipeline_store.create(d.get("name", ""),
                                   d.get("description", ""),
                                   d.get("schedule"))
        return jsonify(p.to_dict())


    @bp.route("/api/pipeline/list", methods=["GET"])
    def api_pipeline_list():
        """列出所有 Pipeline"""
        return jsonify([p.to_dict() for p in pipeline_store.list_all()])


    @bp.route("/api/pipeline/get", methods=["GET"])
    def api_pipeline_get():
        """获取 Pipeline 详情"""
        p = pipeline_store.get(request.args.get("id"))
        if not p:
            return jsonify({"error": "not found"}), 404
        return jsonify(p.to_dict())


    @bp.route("/api/pipeline/delete", methods=["POST"])
    def api_pipeline_delete():
        """删除 Pipeline"""
        pid = (request.get_json(silent=True) or {}).get("id")
        if not pid:
            return jsonify({"error": "缺少参数：id"}), 400
        pipeline_store.delete(pid)
        return jsonify({"ok": True})


    @bp.route("/api/pipeline/node/add", methods=["POST"])
    def api_pipeline_node_add():
        """添加节点"""
        d = request.get_json(silent=True) or {}
        pid = d.get("pipeline_id")
        if not pid:
            return jsonify({"error": "缺少参数：pipeline_id"}), 400
        if not d.get("node_type"):
            return jsonify({"error": "缺少参数：node_type"}), 400
        try:
            node = pipeline_store.add_node(pid, d["node_type"],
                                            d.get("config", {}),
                                            d.get("dependencies"))
            return jsonify(node.to_dict())
        except Exception as e:
            return exc_response(e)


    @bp.route("/api/pipeline/node/remove", methods=["POST"])
    def api_pipeline_node_remove():
        """删除节点"""
        d = request.get_json(silent=True) or {}
        pid, nid = d.get("pipeline_id"), d.get("node_id")
        if not pid or not nid:
            return jsonify({"error": "缺少参数：pipeline_id / node_id"}), 400
        if not pipeline_store.remove_node(pid, nid):
            return jsonify({"error": f"节点不存在：{nid}"}), 404
        return jsonify({"ok": True})


    @bp.route("/api/pipeline/run", methods=["POST"])
    def api_pipeline_run():
        """运行 Pipeline"""
        pid = (request.get_json(silent=True) or {}).get("id")
        if not pid:
            return jsonify({"error": "缺少参数：id"}), 400
        if pipeline_store.get(pid) is None:
            return jsonify({"error": f"Pipeline 不存在：{pid}"}), 404
        try:
            executor = pipeline_store.run_pipeline(pid)
            p = pipeline_store.get(pid)
            return jsonify({"ok": True, "pipeline": p.to_dict(), "logs": executor.logs})
        except Exception as e:
            return exc_response(e)


    @bp.route("/api/pipeline/run/async", methods=["POST"])
    def api_pipeline_run_async():
        """异步运行 Pipeline"""
        pid = (request.get_json(silent=True) or {}).get("id")
        if not pid:
            return jsonify({"error": "缺少参数：id"}), 400
        if pipeline_store.get(pid) is None:
            return jsonify({"error": f"Pipeline 不存在：{pid}"}), 404

        def _bg():
            try:
                pipeline_store.run_pipeline(pid)
            except Exception as e:
                print(f"[Pipeline async] {e}")

        t = threading.Thread(target=_bg, daemon=True)
        t.start()
        return jsonify({"ok": True, "status": "running"})


    # ============================================================


    @bp.route("/api/rag/query", methods=["POST"])
    def api_rag_query():
        body = request.get_json(silent=True) or {}
        question = (body.get("question") or "").strip()
        if not question:
            return jsonify({"ok": False, "error": "缺少参数：question"}), 400
        try:
            top_k = int(body.get("top_k", 5) or 5)
        except (TypeError, ValueError):
            top_k = 5
        from rag_answer import answer_with_knowledge
        return jsonify(answer_with_knowledge(question, top_k=max(1, min(top_k, 50))))


    @bp.route("/api/rag/docs", methods=["GET"])
    def api_rag_docs():
        docs = rag_engine.list_documents()
        return jsonify({"ok": True, "documents": [d.__dict__ for d in docs]})


    @bp.route("/api/rag/delete", methods=["POST"])
    @require_auth_or_api_key
    def api_rag_delete():
        doc_id = (request.get_json(silent=True) or {}).get("doc_id", "")
        if not doc_id:
            return jsonify({"ok": False, "error": "缺少参数：doc_id"}), 400
        rag_engine.delete_document(doc_id)
        return jsonify({"ok": True})

    # ============================================================
    #  Company 全方位分析（公司名 → 多维度分析 → 网页展示 + docx 下载）
    # ============================================================
    @bp.route("/api/company-analysis", methods=["POST"])
    @limiter.limit("3 per minute")
    def api_company_analysis():
        """公司全方位分析。请求体可选 get:false；get=true 时直接下载 docx 文件。
        默认返回结构化报告 JSON 供前端渲染，并附一键下载 docx 的能力。"""
        try:
            data = request.json or {}
            company = (data.get("company") or "").strip()
            want_download = data.get("download") is True
            if not company:
                return jsonify({"error": "请提供公司名称或股票代码", "code": "NO_COMPANY"}), 400

            from company_analysis import analyze_company, generate_docx

            report = analyze_company(company)
            if not report.get("ok"):
                return jsonify({"error": report.get("error", "分析失败")}), 400

            report["ok"] = True
            if want_download:
                docx_bytes = generate_docx(report)
                import io as _io
                buf = _io.BytesIO(docx_bytes)
                return send_file(
                    buf, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    as_attachment=True,
                    download_name=f"{report.get('name', 'company')}_全方位分析报告.docx",
                )
            return jsonify(report)
        except Exception as e:
            return exc_response(e)

    app.register_blueprint(bp)
