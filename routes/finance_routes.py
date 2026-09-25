"""Finance routes — stock data, visualization, CAPM, backtesting, quant models"""
from core_config import exc_response
import io
import re
from datetime import datetime, timedelta
from flask import request, jsonify, send_file, Blueprint
import traceback

from core_config import (
    OUT_DIR, logger, load_dataframe,
    get_tushare_token, get_kline, compute_benchmark_metrics, get_benchmark_returns, INDICES,
    _get_akshare, _get_tushare, _get_csv_file,
    AK_DATA_FUNCS, TS_DATA_FUNCS,
    has_fin_models, has_adv_viz,   # 取用函数：占位符快照会让标志位恒为 False
    # 金融量化函数（core_config 已处理 fallback）
    capm_analysis, backtest_ma_crossover, backtest_macd,
    evaluate_predictions, list_saved_models, load_model,
    predict_with_model, delete_model,
    # 高级可视化函数（core_config 已处理 fallback）
    money_flow_chart, chip_distribution_chart, interactive_heatmap,
    return_curve_chart, max_drawdown_chart, comparison_chart,
    batch_export_charts, get_available_stocks,
    pd, np, os, json, time,
)
from data_lineage import lineage_mgr
from finance_data import normalize_hk_code
from auth import local_guard


def _auto_nav_col(df):
    """自动识别净值列（跳过日期列）"""
    for c in df.columns:
        cl = str(c).lower()
        if any(k in cl for k in ('日期', 'date', '时间', 'time')):
            continue
        if any(k in cl for k in ('单位净值', '累计净值', 'nav', '净值')):
            return c
    for c in df.columns:
        if str(c).lower() in ('close', '收盘', 'price', '价格'):
            return c
    num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    return num_cols[0] if num_cols else None


def _fund_metrics_response(nav, code, source, rf=0.02):
    """净值序列 → 业绩指标（复用 compute_benchmark_metrics + Calmar/Sortino）"""
    nav = pd.to_numeric(nav, errors="coerce").dropna()
    if len(nav) < 2:
        return jsonify({"ok": False, "error": "净值数据不足"}), 400
    rets = nav.pct_change().dropna()
    m = compute_benchmark_metrics(rets)
    annual_ret = float(m.get("annual_return_pct", 0)) / 100.0
    mdd = float(m.get("max_drawdown_pct", 0))
    calmar = round((annual_ret * 100) / abs(mdd), 3) if mdd < 0 else 0.0
    downside = float(rets[rets < 0].std()) if (rets < 0).any() else 0.0
    downside_ann = downside * np.sqrt(252)
    sortino = round((annual_ret - rf) / downside_ann, 2) if downside_ann > 0 else 0.0
    return jsonify({
        "ok": True, "code": code, "source": source, "rows": int(len(nav)),
        "annual_return_pct": m.get("annual_return_pct"), "volatility_pct": m.get("volatility_pct"),
        "max_drawdown_pct": m.get("max_drawdown_pct"), "sharpe_ratio": m.get("sharpe_ratio"),
        "calmar": calmar, "sortino": sortino,
    })


def _safe_fname(fname: str) -> str:
    """下载文件名守卫：仅纯文件名，禁止路径分隔符与 .."""
    name = str(fname).strip()
    if not name or '..' in name or '/' in name or '\\' in name or ':' in name:
        raise ValueError(f'非法文件名: {fname}')
    return name


def _call_with_retry(fn, kwargs, attempts: int = 2, delay: float = 1.0):
    """调用数据接口，对瞬时网络断连做一次短重试。

    背景：东财/新浪等公开接口偶发
    `RemoteDisconnected('Remote end closed connection without response')`，
    重试一次通常恢复；但若对方是限流，重试过多只会加剧封锁，故默认仅 1 次重试。
    参数/字段类错误重试无意义，必须立即抛出，避免把真实缺陷伪装成网络问题。
    """
    last = None
    for i in range(max(1, attempts)):
        try:
            return fn(**kwargs) if kwargs else fn()
        except Exception as e:
            last = e
            name = type(e).__name__
            msg = str(e)
            transient = ('Connection' in name or 'Timeout' in name
                         or 'RemoteDisconnected' in msg or 'Remote end closed' in msg
                         or 'Max retries' in msg or 'ConnectionReset' in name)
            if not transient or i >= attempts - 1:
                raise
            time.sleep(delay * (i + 1))
    if last:
        raise last


def register_routes(app, limiter):
    bp = Blueprint('finance_routes', __name__)
    # 高危面（akshare/tushare 任意函数调用 + 文件读写）：远程请求必须认证
    bp.before_request(local_guard)

    @bp.route("/api/finance/akshare", methods=["POST"])
    def api_akshare():
        data = request.json
        func_name = data.get("func", "").strip()
        if not func_name:
            return jsonify({"error": "请选择数据类型（func）"}), 400

        ak = _get_akshare()
        if ak is None:
            return jsonify({"error": "akshare 未安装或不可用"}), 500

        try:
            func = getattr(ak, func_name, None)
            if func is None:
                return jsonify({"error": f"akshare 无此接口: {func_name}"}), 400

            meta = AK_DATA_FUNCS.get(func_name, {})
            params = meta.get("params", [])

            def _s(key):
                return str(data.get(key, "") or "").strip()

            symbol = _s("symbol")
            period = _s("period") or "daily"
            start_date = _s("start_date")
            end_date = _s("end_date")
            adjust = _s("adjust")            # qfq/hfq/bfq
            indicator = _s("indicator")      # 基金走势 / 板块资金流周期
            date_v = _s("date")
            start_year = _s("start_year")
            extra = data.get("params") or {}
            if not isinstance(extra, dict):
                extra = {}

            # 港股接口：支持 HK+4位 代码（HK0700 → 00700）
            if func_name.startswith("stock_hk_") and symbol:
                _hk = normalize_hk_code(symbol)
                if _hk:
                    symbol = _hk

            # ---- 1) UI 主字段 → kwarg（按 meta.params 声明注入）----
            kwargs = {}
            if symbol:
                for _k in ("symbol", "fund", "ts_code"):
                    if _k in params:
                        kwargs[_k] = symbol
                        break
            if period and "period" in params:
                kwargs["period"] = period
            if start_date and "start_date" in params:
                kwargs["start_date"] = start_date.replace("-", "")
            if end_date and "end_date" in params:
                kwargs["end_date"] = end_date.replace("-", "")
            if indicator and "indicator" in params:
                kwargs["indicator"] = indicator
            if "date" in params:
                _dv = (date_v or end_date or start_date).replace("-", "")
                if _dv:
                    kwargs["date"] = _dv
            if start_year and "start_year" in params:
                kwargs["start_year"] = start_year

            # ---- 2) 高级参数透传（仅限 meta.params 声明过的键，不覆盖 UI 值）----
            for _k, _v in extra.items():
                if _k in params and _k not in kwargs and _v not in (None, ""):
                    kwargs[_k] = _v

            # ---- 3) 接口默认值兜底 ----
            for _k, _v in (meta.get("defaults") or {}).items():
                if _v not in (None, ""):
                    kwargs.setdefault(_k, _v)

            # ---- 4) 复权参数 ----
            if meta.get("adjust") and adjust:
                kwargs["adjust"] = adjust

            df = _call_with_retry(func, kwargs)

            if df is None or (hasattr(df, 'empty') and df.empty):
                return jsonify({"error": "未取到数据，请检查代码/日期/参数是否正确"}), 404

            if hasattr(df, 'to_csv'):
                fname = data.get("filename") or ""
                if not fname:
                    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    fname = f"ak_{func_name}_{symbol or 'data'}_{stamp}.csv"
                if not fname.endswith(".csv"):
                    fname += ".csv"
                dest = OUT_DIR / _safe_fname(fname)
                df.to_csv(dest, index=False, encoding="utf-8-sig")
                try:
                    lineage_mgr.record("fetch", "akshare:" + func_name, fname,
                                       detail=f"{len(df)}行")
                except Exception:
                    pass

                preview = []
                if len(df) > 0:
                    preview = df.head(5).fillna("").astype(str).to_dict(orient="records")

                return jsonify({
                    "ok": True,
                    "rows": len(df),
                    "cols": len(df.columns),
                    "file": fname,
                    "preview": preview,
                })
            else:
                return jsonify({"error": "该接口未返回表格数据（DataFrame）"}), 500

        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/kline", methods=["POST"])
    def api_finance_kline():
        """
        获取 K 线数据（支持复权）
        使用 finance_data.py 模块获取数据
        """
        data = request.json
        symbol = data.get("symbol", "").strip()
        period = data.get("period", "daily").strip()
        start = data.get("start_date", data.get("start", "")).strip()
        end = data.get("end_date", data.get("end", "")).strip()
        adjust = data.get("adjust", "qfq").strip()  # 默认前复权

        if not symbol:
            return jsonify({"error": "symbol 不能为空"}), 400

        start = start.replace("-", "")
        end = end.replace("-", "")

        try:
            df = get_kline(symbol=symbol, period=period, start=start, end=end, adjust=adjust)
        except ImportError:
            return jsonify({"error": "akshare 未安装，请执行: pip install akshare"}), 500

        if df is None or df.empty:
            return jsonify({"error": "获取数据失败"}), 404

        fname = data.get("filename", f"kline_{symbol}_{adjust}.csv")
        if not fname.endswith(".csv"):
            fname += ".csv"
        dest = OUT_DIR / _safe_fname(fname)
        df.to_csv(dest, index=False, encoding="utf-8-sig")
        try:
            lineage_mgr.record("fetch", "akshare:kline", fname,
                               detail=f"{symbol} {period} {adjust}，{len(df)}行")
        except Exception:
            pass

        preview = df.head(5).fillna("").astype(str).to_dict(orient="records")

        return jsonify({
            "ok": True,
            "rows": len(df),
            "cols": len(df.columns),
            "file": fname,
            "symbol": symbol,
            "adjust": adjust,
            "preview": preview,
            "source": "akshare",
        })

    @bp.route("/api/finance/tushare", methods=["POST"])
    def api_tushare():
        data = request.json
        func_name = data.get("func", "").strip()
        token = data.get("token", "").strip()
        if not func_name:
            return jsonify({"error": "请选择数据类型（func）"}), 400
        # Priority: user input > encrypted storage > env var
        if not token:
            token = get_tushare_token()
        if not token:
            return jsonify({"error": "缺少 tushare token，请填写或先在设置中保存"}), 400

        ts = _get_tushare()
        if ts is None:
            return jsonify({"error": "tushare 未安装或不可用"}), 500

        try:
            pro = ts.pro_api(token)
            func = getattr(pro, func_name, None)
            if func is None:
                return jsonify({"error": f"tushare 无此接口: {func_name}"}), 400

            def _s(key):
                return str(data.get(key, "") or "").strip()

            ts_code = _s("ts_code")
            start_date = _s("start_date")
            end_date = _s("end_date")
            trade_date = _s("trade_date")
            extra = data.get("params") or {}
            if not isinstance(extra, dict):
                extra = {}

            meta = TS_DATA_FUNCS.get(func_name, {})
            params = meta.get("params", [])

            kwargs = {}
            if "ts_code" in params and ts_code:
                kwargs["ts_code"] = ts_code
            if "start_date" in params and start_date:
                kwargs["start_date"] = start_date.replace("-", "")
            if "end_date" in params and end_date:
                kwargs["end_date"] = end_date.replace("-", "")
            if "trade_date" in params:
                kwargs["trade_date"] = (trade_date or end_date
                                        or datetime.now().strftime("%Y%m%d")).replace("-", "")

            # 高级参数透传（仅限 meta.params 声明过的键）
            for _k, _v in extra.items():
                if _k in params and _k not in kwargs and _v not in (None, ""):
                    kwargs[_k] = _v

            df = func(**kwargs)

            if df is None or df.empty:
                return jsonify({"error": "未取到数据，请检查代码/日期或该接口的积分权限"}), 404

            fname = data.get("filename") or ""
            if not fname:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                fname = f"ts_{func_name}_{ts_code or 'data'}_{stamp}.csv"
            if not fname.endswith(".csv"):
                fname += ".csv"
            dest = OUT_DIR / _safe_fname(fname)
            df.to_csv(dest, index=False, encoding="utf-8-sig")
            try:
                lineage_mgr.record("fetch", "tushare:" + func_name, fname,
                                   detail=f"{len(df)}行")
            except Exception:
                pass

            preview = df.head(5).fillna("").astype(str).to_dict(orient="records")

            return jsonify({
                "ok": True,
                "rows": len(df),
                "cols": len(df.columns),
                "file": fname,
                "preview": preview,
                "source": "tushare",
            })

        except Exception as e:
            return exc_response(e)

    def _fetch_source_csv(df, source_id, func_name, data, default_prefix):
        """统一落盘 + lineage + preview（yfinance / fred 共用）"""
        if df is None or (hasattr(df, "empty") and df.empty):
            return jsonify({"error": "暂无数据"}), 404
        fname = data.get("filename") or ""
        if not fname:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            sym = (data.get("symbol") or data.get("series_id") or data.get("func") or "data").strip()
            sym = "".join(ch for ch in sym if ch.isalnum() or ch in ("-", "_", ".")) or "data"
            fname = f"{default_prefix}_{func_name}_{sym}_{stamp}.csv"
        if not fname.endswith(".csv"):
            fname += ".csv"
        dest = OUT_DIR / _safe_fname(fname)
        df.to_csv(dest, index=False, encoding="utf-8-sig")
        try:
            lineage_mgr.record("fetch", f"{source_id}:{func_name}", fname, detail=f"{len(df)}行")
        except Exception:
            pass
        preview = []
        if len(df) > 0:
            preview = df.head(5).fillna("").astype(str).to_dict(orient="records")
        return jsonify({
            "ok": True,
            "rows": len(df),
            "cols": len(df.columns),
            "file": fname,
            "preview": preview,
            "source": source_id,
        })

    @bp.route("/api/finance/yfinance", methods=["POST"])
    def api_finance_yfinance():
        """yfinance 数据源（免费、无需 token）：美股/全球/外汇/美债/新闻"""
        data = request.json or {}
        func_name = data.get("func", "").strip()
        if not func_name:
            return jsonify({"error": "请选择数据类型"}), 400
        try:
            from yfinance_data import YF_FUNCS
            meta = YF_FUNCS.get(func_name)
            if meta is None:
                return jsonify({"error": f"yfinance 不支持的类型: {func_name}"}), 400

            kwargs = {}
            params = meta.get("params", [])
            if "symbol" in params:
                kwargs["symbol"] = data.get("symbol", "").strip()
            if "period" in params:
                kwargs["period"] = data.get("period", "daily").strip()
            if "start_date" in params:
                kwargs["start_date"] = data.get("start_date", "").strip()
            if "end_date" in params:
                kwargs["end_date"] = data.get("end_date", "").strip()
            if "limit" in params:
                try:
                    kwargs["limit"] = int(data.get("limit", 10) or 10)
                except (TypeError, ValueError):
                    kwargs["limit"] = 10

            # fx/kline 等允许 symbol 为空走默认快照
            if func_name == "fx" and not kwargs.get("symbol"):
                kwargs.pop("symbol", None)
            if func_name in ("treasury", "indices") and not kwargs.get("symbol"):
                kwargs.pop("symbol", None)

            df = meta["func"](**kwargs)
            return _fetch_source_csv(df, "yfinance", func_name, data, "yf")
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/fred", methods=["POST"])
    def api_finance_fred():
        """FRED 宏观/利率数据源（序列取数免 token；可选 FRED_API_KEY 增强搜索）"""
        data = request.json or {}
        func_name = data.get("func", "").strip()
        if not func_name:
            return jsonify({"error": "请选择序列或功能"}), 400
        try:
            from fred_data import FRED_FUNCS, get_series, search_series, list_popular, get_series_pack

            start = (data.get("start_date") or "").strip()
            end = (data.get("end_date") or "").strip()
            query = (data.get("query") or data.get("symbol") or "").strip()

            # 直接填序列 ID（catalog 中每个序列也是一个 func 值）
            if func_name not in FRED_FUNCS:
                df = get_series(func_name, start_date=start, end_date=end)
                return _fetch_source_csv(df, "fred", func_name, data, "fred")

            if func_name == "series":
                sid = (data.get("series_id") or data.get("symbol") or "").strip()
                if not sid:
                    return jsonify({"error": "请提供 FRED 序列 ID（如 DGS10）"}), 400
                df = get_series(sid, start_date=start, end_date=end)
                return _fetch_source_csv(df, "fred", sid, data, "fred")
            if func_name == "search":
                df = search_series(query)
                return _fetch_source_csv(df, "fred", "search", data, "fred")
            if func_name == "list":
                df = list_popular()
                return _fetch_source_csv(df, "fred", "list", data, "fred")
            if func_name == "pack":
                ids_raw = data.get("series_ids") or []
                if isinstance(ids_raw, str):
                    ids_raw = [x for x in re.split(r"[,\s;]+", ids_raw) if x]
                df = get_series_pack(ids_raw or None, start_date=start, end_date=end)
                return _fetch_source_csv(df, "fred", "pack", data, "fred")

            return jsonify({"error": f"fred 不支持的类型: {func_name}"}), 400
        except Exception as e:
            return exc_response(e)

    # ==============================
    #  金融数据建模和可视化 API
    # ==============================

    @bp.route("/api/finance/baostock", methods=["POST"])
    def api_finance_baostock():
        """baostock 金融数据源（免费、无限频、无需 token）"""
        data = request.json or {}
        func_name = data.get("func", "").strip()
        if not func_name:
            return jsonify({"error": "请选择数据类型"}), 400

        try:
            from baostock_data import BAOSTOCK_FUNCS
            meta = BAOSTOCK_FUNCS.get(func_name)
            if meta is None:
                return jsonify({"error": f"baostock 不支持的类型: {func_name}"}), 400

            kwargs = {}
            params = meta.get("params", [])
            if "symbol" in params:
                kwargs["symbol"] = data.get("symbol", "").strip()
            if "period" in params:
                kwargs["period"] = data.get("period", "daily").strip()
            if "start_date" in params:
                kwargs["start_date"] = data.get("start_date", "").strip()
            if "end_date" in params:
                kwargs["end_date"] = data.get("end_date", "").strip()
            if "adjust" in params:
                kwargs["adjust"] = data.get("adjust", "qfq").strip()
            if "index" in params:
                kwargs["index"] = data.get("index", "300").strip()
            if "year" in params:
                try:
                    kwargs["year"] = int(data.get("year", 0) or 0)
                except (TypeError, ValueError):
                    kwargs["year"] = 0
            if "quarter" in params:
                try:
                    kwargs["quarter"] = int(data.get("quarter", 4) or 4)
                except (TypeError, ValueError):
                    kwargs["quarter"] = 4
            if "report_type" in params:
                kwargs["report_type"] = data.get("report_type", "profit").strip()

            # 财务类统一走 get_financial
            if func_name in ('profit', 'balance', 'cashflow', 'growth'):
                kwargs['report_type'] = func_name

            df = meta["func"](**kwargs)
            if df is None or (hasattr(df, 'empty') and df.empty):
                return jsonify({"error": "暂无数据"}), 404

            fname = data.get("filename") or ""
            if not fname:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                bs_sym = (data.get("symbol") or "data").strip() or "data"
                fname = f"bs_{func_name}_{bs_sym}_{stamp}.csv"
            if not fname.endswith(".csv"):
                fname += ".csv"
            dest = OUT_DIR / _safe_fname(fname)
            df.to_csv(dest, index=False, encoding="utf-8-sig")
            try:
                lineage_mgr.record("fetch", "baostock:" + func_name, fname,
                                   detail=f"{len(df)}行")
            except Exception:
                pass

            preview = []
            if len(df) > 0:
                preview = df.head(5).fillna("").astype(str).to_dict(orient="records")

            return jsonify({
                "ok": True,
                "rows": len(df),
                "cols": len(df.columns),
                "file": fname,
                "preview": preview,
                "source": "baostock",
            })
        except Exception as e:
            return exc_response(e)

    @app.route("/api/finance/fetch-stock", methods=["POST"])
    def api_finance_fetch_stock():
        """按股票代码或名称获取 A 股日K行情并保存到 output（供 AI 助手自动取数分析）"""
        try:
            from ai_assistant import resolve_stock_symbol
            data = request.json or {}
            raw = (data.get("symbol") or "").strip()
            if not raw:
                return jsonify({"error": "请提供股票代码或名称"}), 400
            sym = resolve_stock_symbol(raw)
            if not sym:
                return jsonify({"error": f"未识别到有效的 A 股标的: {raw}"}), 404
            code = sym['code']
            name = sym.get('name') or ''

            ak = _get_akshare()
            if ak is None:
                return jsonify({"error": "akshare 未安装"}), 500

            end = datetime.now().strftime('%Y%m%d')
            start = (datetime.now() - timedelta(days=730)).strftime('%Y%m%d')
            df = ak.stock_zh_a_hist(symbol=code, period='daily',
                                    start_date=start, end_date=end, adjust='qfq')
            if df is None or df.empty:
                return jsonify({"error": f"暂无 {code} 行情数据"}), 404

            fname = f"stock_{code}.csv"  # 同名覆盖，避免文件堆积
            dest = OUT_DIR / _safe_fname(fname)
            df.to_csv(dest, index=False, encoding="utf-8-sig")

            preview = []
            if len(df) > 0:
                preview = df.tail(3).fillna("").astype(str).to_dict(orient="records")
            return jsonify({
                "ok": True,
                "file": fname,
                "code": code,
                "name": name,
                "rows": len(df),
                "cols": len(df.columns),
                "preview": preview,
            })
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/viz", methods=["POST"])
    def api_finance_viz():
        """金融数据可视化 - 支持多图表选择"""
        data = request.json
        selected_charts = data.get("charts", ["candle"])

        try:
            filepath = _get_csv_file(data, required_cols=["close"])
            if not filepath:
                return jsonify({"error": "请先获取金融数据（需包含收盘价列）"}), 400

            # 读取数据
            df = pd.read_csv(filepath)

            # 导入建模模块
            from finance_modeler import FinanceModeler

            modeler = FinanceModeler()
            modeler.load_data(df)
            modeler.add_technical_indicators()

            # 生成选中的图表
            figures = []

            chart_funcs = {
                'candle': ('K线图 (含成交量)', modeler.create_candlestick_chart),
                'line': ('价格走势', modeler.create_ma_chart),
                'volume': ('成交量分析', modeler.create_volume_chart),
                'ma': ('移动平均线', modeler.create_ma_chart),
                'macd': ('MACD指标', modeler.create_macd_chart),
                'rsi': ('RSI相对强弱指标', modeler.create_rsi_chart),
                'boll': ('布林带', modeler.create_bollinger_chart),
                'heatmap': ('相关性热力图', modeler.create_correlation_heatmap),
                'returns': ('收益率分布', modeler.create_returns_distribution),
                'bar': ('柱状图（各列数值分布）', modeler.create_bar_chart),
                'box': ('箱线图（异常值检测）', modeler.create_box_plot),
                'density': ('密度图（数值分布）', modeler.create_density_plot),
            }

            for chart_type in selected_charts:
                if chart_type in chart_funcs:
                    name, func = chart_funcs[chart_type]
                    try:
                        img_data = func(df)
                        figures.append({'name': name, 'data': img_data})
                    except Exception as e:
                        logger.error(f"图表 {name} 生成失败: {e}")

            return jsonify({
                'ok': True,
                'figures': figures
            })

        except Exception as e:
            return exc_response(e)

    # ==================== 开放基金业绩指标 API ====================

    @bp.route("/api/finance/fund-metrics", methods=["POST"])
    def api_fund_metrics():
        """计算开放基金业绩指标（夏普比率、最大回撤、年化收益、卡玛、索提诺等）
        三种输入：code（基金代码拉净值）/ csv（内联净值文本）/ file（output 目录文件）
        """
        try:
            from finance_modeler import FinanceModeler
            data = request.json or {}
            code = (data.get("code") or data.get("symbol") or "").strip()
            inline_csv = data.get("csv")
            rf = float(data.get("rf", 0.02))

            # ---- 方式1: 基金代码 → 拉历史净值 ----
            if code:
                source = (data.get("source") or "akshare").strip().lower()
                start_date = str(data.get("start_date") or "").replace("-", "")
                end_date = str(data.get("end_date") or "").replace("-", "")
                if source == "akshare":
                    ak = _get_akshare()
                    if ak is None:
                        return jsonify({"ok": False, "error": "akshare 未安装/不可用"}), 500
                    df = ak.fund_open_fund_info_em(symbol=code, indicator="单位净值走势")
                    if df is None or hasattr(df, 'empty') and df.empty:
                        return jsonify({"ok": False, "error": "未取到净值数据，请检查基金代码"}), 404
                    nav_col = "单位净值" if "单位净值" in df.columns else _auto_nav_col(df)
                    if nav_col is None:
                        return jsonify({"ok": False, "error": "净值列识别失败"}), 400
                    nav = df[nav_col]
                else:  # tushare
                    ts = _get_tushare()
                    if ts is None:
                        return jsonify({"ok": False, "error": "tushare 未安装/无 token"}), 500
                    kw = {"ts_code": f"{code}.OF"}
                    if start_date: kw["start_date"] = start_date
                    if end_date: kw["end_date"] = end_date
                    df = ts.pro_api().fund_nav(**kw)
                    if df is None or hasattr(df, 'empty') and df.empty:
                        return jsonify({"ok": False, "error": "未取到净值数据，请检查基金代码/token"}), 404
                    nav = df["unit_nav"]
                return _fund_metrics_response(nav, code, source, rf)

            # ---- 方式2: 内联 CSV ----
            if inline_csv:
                df = pd.read_csv(io.StringIO(inline_csv))
                nav_col = _auto_nav_col(df)
                if nav_col is None:
                    return jsonify({"ok": False, "error": "CSV 中未找到净值列"}), 400
                return _fund_metrics_response(df[nav_col], "csv", "csv", rf)

            # ---- 方式3: 上传文件（原逻辑）----
            file_name = data.get("file") or data.get("file_name")
            if not file_name:
                return jsonify({"error": "请提供基金代码(code)、净值CSV(csv) 或文件(file)"}), 400
            path = OUT_DIR / file_name
            if not path.exists():
                return jsonify({"error": "文件不存在"}), 404

            # 读取 CSV / Excel
            if path.suffix.lower() == '.csv':
                df = pd.read_csv(path)
            else:
                df = pd.read_excel(path)

            modeler = FinanceModeler()
            modeler.load_data(df)
            nav_col = data.get("nav_col", "") or None
            rf = float(data.get("rf", 0.02))
            metrics = modeler.fund_performance_metrics(nav_col=nav_col, rf=rf)
            return jsonify(metrics)
        except Exception as e:
            return exc_response(e)

    # ==================== 高级金融可视化 API ====================

    @bp.route("/api/finance/viz-stocks", methods=["GET"])
    def api_viz_stocks():
        """获取可用的股票数据文件列表（用于对比图）"""
        try:
            files = get_available_stocks(OUT_DIR)
            return jsonify({"ok": True, "stocks": files})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/viz-advanced", methods=["POST"])
    def api_viz_advanced():
        """高级交互式图表"""
        if not has_adv_viz():
            return jsonify({"error": "finviz_advanced 模块未加载"}), 500

        data = request.json
        chart_types = data.get("charts", ["money_flow"])
        styles = data.get("styles", {})

        try:
            filepath = _get_csv_file(data, required_cols=["close"])
            if not filepath:
                return jsonify({"error": "请先获取金融数据（需包含收盘价列）"}), 400

            results = []

            chart_funcs = {
                'money_flow': ('资金流向', money_flow_chart),
                'chip': ('筹码分布', chip_distribution_chart),
                'heatmap': ('相关性热力图', lambda fp, **kw: interactive_heatmap(fp, method='pearson', **kw)),
                'return_curve': ('收益曲线', return_curve_chart),
                'drawdown': ('最大回撤', max_drawdown_chart),
            }

            for ct in chart_types:
                if ct in chart_funcs:
                    name, func = chart_funcs[ct]
                    try:
                        html, stats = func(filepath, **styles)
                        results.append({'name': name, 'html': html, 'stats': stats})
                    except Exception as e:
                        results.append({'name': name, 'error': str(e)})

            return jsonify({"ok": True, "charts": results})

        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/viz-compare", methods=["POST"])
    def api_viz_compare():
        """多股对比图"""
        if not has_adv_viz():
            return jsonify({"error": "finviz_advanced 模块未加载"}), 500

        data = request.json
        stock_files = data.get("stocks", [])
        metric = data.get("metric", "close")
        styles = data.get("styles", {})

        if not stock_files:
            return jsonify({"error": "请选择至少一只股票"}), 400

        try:
            html = comparison_chart(stock_files, metric=metric, **styles)
            return jsonify({"ok": True, "html": html})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/viz-batch-export", methods=["POST"])
    def api_viz_batch_export():
        """批量导出图表为 HTML 压缩包"""
        if not has_adv_viz():
            return jsonify({"error": "finviz_advanced 模块未加载"}), 500

        data = request.json
        chart_types = data.get("charts", ["money_flow", "chip", "heatmap", "return_curve", "drawdown"])

        try:
            filepath = _get_csv_file(data, required_cols=["close"])
            if not filepath:
                return jsonify({"error": "请先获取金融数据（需包含收盘价列）"}), 400

            zip_buf = batch_export_charts(filepath, chart_types)
            return send_file(
                io.BytesIO(zip_buf.read()),
                mimetype='application/zip',
                as_attachment=True,
                download_name=f'charts_{datetime.now().strftime("%Y%m%d_%H%M%S")}.zip'
            )
        except Exception as e:
            return exc_response(e)

    # ==================== 金融模型增强 API ====================

    @bp.route("/api/finance/capm", methods=["POST"])
    def api_finance_capm():
        """CAPM 分析"""
        if not has_fin_models():
            return jsonify({"error": "finance_quant 模块未加载"}), 500
        try:
            data = request.json
            filepath = _get_csv_file(data, required_cols=["close"])
            if not filepath:
                return jsonify({"error": "请先获取金融数据（需包含收盘价列）"}), 400
            result = capm_analysis(filepath,
                                   close_col=data.get("close_col"),
                                   rf_rate=float(data.get("rf_rate", 0.02)))
            return jsonify({"ok": True, **result})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/fama-french", methods=["POST"])
    def api_finance_fama_french():
        """Fama-French 五因子模型（数据源：baostock）"""
        data = request.json or {}
        try:
            from factor_model import run_fama_french
            result = run_fama_french(
                max_stocks=int(data.get("max_stocks", 30)),
                months=int(data.get("months", 12)),
                rf_annual=float(data.get("rf_annual", 0.02)))
            return jsonify(result)
        except Exception as e:
            logger.error(f"fama-french error: {e}")
            return exc_response(e)

    @bp.route("/api/finance/backtest", methods=["POST"])
    def api_finance_backtest():
        """策略回测"""
        if not has_fin_models():
            return jsonify({"error": "finance_quant 模块未加载"}), 500
        try:
            data = request.json
            strategy = data.get("strategy", "ma")
            filepath = _get_csv_file(data, required_cols=["close"])
            if not filepath:
                return jsonify({"error": "请先获取金融数据（需包含收盘价列）"}), 400

            fast = int(data.get("fast", 5))
            slow = int(data.get("slow", 20))
            signal = int(data.get("signal", 9))
            benchmark = data.get("benchmark", "000300")
            run_brinson = data.get("brinson", False)

            if strategy == "macd":
                result = backtest_macd(filepath, close_col=data.get("close_col"), fast=12, slow=26, signal=9, benchmark=benchmark)
            else:
                result = backtest_ma_crossover(filepath, close_col=data.get("close_col"), fast=fast, slow=slow, benchmark=benchmark)

            if run_brinson:
                try:
                    import pandas as pd
                    from brinson import simple_backtest_attribution
                    df = pd.read_csv(filepath)
                    date_col = 'date' if 'date' in df.columns else '日期'
                    close_col = 'close' if 'close' in df.columns else '收盘'
                    if date_col in df.columns:
                        df[date_col] = pd.to_datetime(df[date_col])
                        df = df.sort_values(date_col)
                    returns = df[close_col].pct_change().dropna()
                    bench_start = str(df[date_col].iloc[0])[:10].replace('-','') if date_col in df.columns else ''
                    bench_end = str(df[date_col].iloc[-1])[:10].replace('-','') if date_col in df.columns else ''
                    from benchmark import get_benchmark_returns
                    bench_returns = get_benchmark_returns(benchmark, bench_start, bench_end)
                    if bench_returns is not None and len(bench_returns) > 0:
                        bench_returns = bench_returns.reindex(returns.index, method='ffill').fillna(0)
                        brinson_result = simple_backtest_attribution(returns, bench_returns, num_segments=min(5, len(returns)//10+1))
                        result['brinson'] = brinson_result.to_dict()
                except Exception as e:
                    result['brinson_error'] = str(e)

            return jsonify({"ok": True, **result})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/predict-eval", methods=["POST"])
    def api_finance_predict_eval():
        """预测模型评估对比"""
        if not has_fin_models():
            return jsonify({"error": "finance_quant 模块未加载"}), 500
        try:
            data = request.json
            filepath = _get_csv_file(data, required_cols=["close"])
            if not filepath:
                return jsonify({"error": "请先获取金融数据（需包含收盘价列）"}), 400
            result = evaluate_predictions(filepath,
                                          close_col=data.get("close_col"),
                                          test_size=float(data.get("test_size", 0.3)),
                                          model_type=data.get("model_type", "linear"),
                                          predict_days=int(data.get("predict_days", 30)))
            return jsonify({"ok": True, **result})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/model-list", methods=["GET"])
    def api_model_list():
        """列出已保存的模型"""
        if not has_fin_models():
            return jsonify({"models": []})
        try:
            models = list_saved_models()
            return jsonify({"ok": True, "models": models})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/model-predict", methods=["POST"])
    def api_model_predict():
        """使用保存的模型预测"""
        if not has_fin_models():
            return jsonify({"error": "finance_quant 模块未加载"}), 500
        try:
            data = request.json
            filepath = _get_csv_file(data, required_cols=["close"])
            if not filepath:
                return jsonify({"error": "请先获取金融数据（需包含收盘价列）"}), 400

            model_data = load_model(data.get("model_name"))
            result = predict_with_model(model_data, filepath,
                                        close_col=data.get("close_col"),
                                        predict_days=int(data.get("predict_days", 30)))
            return jsonify({"ok": True, **result})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/model-delete", methods=["POST"])
    def api_model_delete():
        """删除保存的模型"""
        if not has_fin_models():
            return jsonify({"error": "finance_quant 模块未加载"}), 500
        data = request.get_json(silent=True) or {}
        model_name = (data.get("model_name") or "").strip()
        if not model_name:
            return jsonify({"ok": False, "error": "缺少参数：model_name"}), 400
        try:
            ok = delete_model(model_name)
            if not ok:
                return jsonify({"ok": False, "error": f"模型不存在或名称非法：{model_name}"}), 404
            return jsonify({"ok": True})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/finance/catalog", methods=["GET"])
    def api_finance_catalog():
        """数据源目录：按来源/分类返回全部可用接口，供前端下拉与 AI 工具发现。

        返回结构:
            {ok, sources: [{id, name, needs_token, funcs:[{func,label,desc,cat,params}]}]}
        """
        def _pack(table):
            groups = {}
            for fn, meta in table.items():
                cat = meta.get("cat") or "其他"
                groups.setdefault(cat, []).append({
                    "func": fn,
                    "label": meta.get("label") or fn,
                    "desc": meta.get("desc") or "",
                    "params": meta.get("params") or [],
                })
            out = []
            for cat in sorted(groups):
                out.append({"cat": cat, "funcs": groups[cat]})
            return out

        sources = [{
            "id": "akshare",
            "name": "AkShare（免费 · 无需 token）",
            "needs_token": False,
            "count": len(AK_DATA_FUNCS),
            "groups": _pack(AK_DATA_FUNCS),
        }, {
            "id": "baostock",
            "name": "Baostock（免费 · 无需 token · 无限频）",
            "needs_token": False,
            "count": None,
            "groups": [],
        }, {
            "id": "tushare",
            "name": "Tushare Pro（需 token · 按积分权限）",
            "needs_token": True,
            "count": len(TS_DATA_FUNCS),
            "groups": _pack(TS_DATA_FUNCS),
        }, {
            "id": "yfinance",
            "name": "yfinance（免费 · 无需 token · 美股/全球/外汇/美债）",
            "needs_token": False,
            "count": None,
            "groups": [],
        }, {
            "id": "fred",
            "name": "FRED（免费 · 美国宏观与利率 · 序列免 token）",
            "needs_token": False,
            "count": None,
            "groups": [],
        }]

        try:
            from baostock_data import BAOSTOCK_FUNCS
            bs_list = [{"func": fn, "label": meta.get("desc") or fn,
                        "desc": meta.get("desc") or "", "params": meta.get("params") or []}
                       for fn, meta in BAOSTOCK_FUNCS.items()]
            sources[1]["count"] = len(bs_list)
            sources[1]["groups"] = [{"cat": "Baostock", "funcs": bs_list}]
        except Exception:
            pass

        try:
            from yfinance_data import YF_FUNCS
            yf_groups = {}
            for fn, meta in YF_FUNCS.items():
                cat = meta.get("cat") or "其他"
                yf_groups.setdefault(cat, []).append({
                    "func": fn,
                    "label": meta.get("label") or fn,
                    "desc": meta.get("desc") or "",
                    "params": meta.get("params") or [],
                })
            sources[3]["count"] = len(YF_FUNCS)
            sources[3]["groups"] = [{"cat": c, "funcs": yf_groups[c]} for c in sorted(yf_groups)]
        except Exception:
            pass

        try:
            from fred_data import FRED_SERIES, FRED_FUNCS
            # 精选序列作为可选项（func=序列 ID），另附检索/目录/多序列功能
            fred_groups = {}
            for sid, meta in FRED_SERIES.items():
                cat = meta.get("cat") or "其他"
                fred_groups.setdefault(cat, []).append({
                    "func": sid,
                    "label": f"{meta.get('label') or sid}（{sid}）",
                    "desc": meta.get("desc") or "",
                    "params": ["start_date", "end_date"],
                })
            base_funcs = [{
                "func": fn,
                "label": meta.get("label") or fn,
                "desc": meta.get("desc") or "",
                "params": meta.get("params") or [],
            } for fn, meta in FRED_FUNCS.items()]
            groups = [{"cat": "功能", "funcs": base_funcs}]
            for c in sorted(fred_groups):
                groups.append({"cat": c, "funcs": fred_groups[c]})
            sources[4]["count"] = len(FRED_SERIES) + len(FRED_FUNCS)
            sources[4]["groups"] = groups
        except Exception:
            pass

        return jsonify({"ok": True,
                        "total": sum(s["count"] or 0 for s in sources),
                        "sources": sources})

    @bp.route("/api/finance/model", methods=["POST"])
    def api_finance_model():
        """金融数据建模 & 预测"""
        data = request.json
        model_type = data.get("model_type", "linear")
        predict_days = int(data.get("predict_days", 5))

        try:
            filepath = _get_csv_file(data, required_cols=["close"])
            if not filepath:
                return jsonify({"error": "请先获取金融数据（需包含收盘价列）"}), 400

            df = pd.read_csv(filepath)

            from finance_modeler import FinanceModeler

            modeler = FinanceModeler()
            modeler.load_data(df)
            modeler.add_technical_indicators()

            metrics = modeler.train_model(model_type=model_type)
            predictions = modeler.predict_future(days=predict_days)
            pred_chart = modeler.create_prediction_chart(predictions=predictions)

            result = {
                "ok": True,
                "model_name": model_type.upper(),
                "rmse": float(metrics["rmse"]),
                "mae": float(metrics["mae"]),
                "r2": float(metrics["r2"]),
                "predictions": [float(p) for p in predictions],
                "figures": [{"name": "价格预测", "data": pred_chart}],
            }

            return jsonify(result)

        except Exception as e:
            return exc_response(e)
    app.register_blueprint(bp)
