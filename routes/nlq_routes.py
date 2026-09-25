"""NLQ routes — natural language query, A/B testing, bad case collection"""
from flask import request, jsonify, Blueprint

from core_config import logger
from core_config import exc_response
from nlq_router import nlq_execute
from auth import local_guard


def register_routes(app, limiter):
    bp = Blueprint('nlq_routes', __name__)
    # 高危面（nlq 可触发回测/报告生成并读文件）：远程请求必须认证
    bp.before_request(local_guard)
    """Register all /api/nlq/*, /api/abtest/*, and /api/badcase/* routes on the Flask app."""

    # ============================================================
    #  /api/nlq/execute  (POST)
    # ============================================================
    @bp.route("/api/nlq/execute", methods=["POST"])
    def api_nlq_execute():
        """NLQ 路由入口：接收 JSON 指令，安全调用"""
        data = request.json
        result = nlq_execute(data)
        return jsonify(result)

    # ============================================================
    #  /api/nlq/query  (POST)
    # ============================================================
    @bp.route("/api/nlq/query", methods=["POST"])
    @limiter.limit("60 per minute")
    def api_nlq_query():
        """自然语言查数：真实表结构 + LLM 生成只读 SQL → 预览（默认）或执行。"""
        from nlq_answer import answer
        data = request.get_json(silent=True) or {}
        question = (data.get("question") or data.get("query") or "").strip()
        statement = (data.get("sql") or "").strip()
        if not question and not statement:
            return jsonify({"ok": False, "error": "缺少参数：question"}), 400
        try:
            result = answer(question, db_path=data.get("db_path"),
                            conn_id=data.get("conn_id"), source=data.get("source"),
                            preview=bool(data.get("preview")), sql=statement or None)
        except Exception as e:
            return exc_response(e)
        return jsonify(result)

    # ============================================================
    #  /api/nlq/sources  (GET)
    # ============================================================
    @bp.route("/api/nlq/sources", methods=["GET"])
    def api_nlq_sources():
        """可查数据源清单：output/ 下的 SQLite 文件 + 已保存的数据库连接"""
        from nlq_answer import discover_sources
        return jsonify({"ok": True, "sources": discover_sources()})

    # ============================================================
    #  /api/nlq/feedback  (POST)
    # ============================================================
    @bp.route("/api/nlq/feedback", methods=["POST"])
    def api_nlq_feedback():
        """用户反馈接口（重新生成/结果不准确）"""
        from feedback_loop import FeedbackDrivenNLQ
        data = request.json
        feedback_nlq = FeedbackDrivenNLQ()
        feedback_nlq.report_feedback(
            session_id=data.get("session_id", "default"),
            feedback_type=data.get("feedback_type", "regenerate"),
            query=data.get("query", ""),
            sql=data.get("sql", "")
        )
        return jsonify({"ok": True})

    # ============================================================
    #  /api/abtest/report  (GET)
    # ============================================================
    @bp.route("/api/abtest/report", methods=["GET"])
    def api_abtest_report():
        """A/B 测试报告"""
        from feedback_loop import PromptABTestManager
        manager = PromptABTestManager()
        return jsonify(manager.get_comparison_report())

    # ============================================================
    #  /api/abtest/register  (POST)
    # ============================================================
    @bp.route("/api/abtest/register", methods=["POST"])
    def api_abtest_register():
        """注册新 Prompt 版本"""
        from feedback_loop import PromptABTestManager
        data = request.json
        manager = PromptABTestManager()
        version = manager.register_version(
            version_id=data.get("version_id"),
            name=data.get("name", ""),
            prompt_template=data.get("prompt_template", ""),
            traffic_percent=data.get("traffic_percent", 0)
        )
        return jsonify({"ok": True, "version": version.version_id})

    # ============================================================
    #  /api/abtest/set_traffic  (POST)
    # ============================================================
    @bp.route("/api/abtest/set_traffic", methods=["POST"])
    def api_abtest_set_traffic():
        """设置流量分配"""
        from feedback_loop import PromptABTestManager
        data = request.json
        manager = PromptABTestManager()
        manager.set_traffic(data.get("version_id"), data.get("percent", 0))
        return jsonify({"ok": True})

    # ============================================================
    #  /api/abtest/promote  (POST)
    # ============================================================
    @bp.route("/api/abtest/promote", methods=["POST"])
    def api_abtest_promote():
        """提升某版本为 100% 流量"""
        from feedback_loop import PromptABTestManager
        data = request.json
        manager = PromptABTestManager()
        manager.promote_version(data.get("version_id"))
        return jsonify({"ok": True})

    # ============================================================
    #  /api/badcase/stats  (GET)
    # ============================================================
    @bp.route("/api/badcase/stats", methods=["GET"])
    def api_badcase_stats():
        """Bad Case 统计"""
        from feedback_loop import BadCaseCollector
        collector = BadCaseCollector()
        return jsonify(collector.get_stats())
    app.register_blueprint(bp)
