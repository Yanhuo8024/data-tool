"""Tools routes — table operations, time series, hypothesis testing, financial statements, screener, NLP"""
from core_config import exc_response
import io
import base64
import traceback
from pathlib import Path

from flask import request, jsonify, Blueprint

from core_config import (
    OUT_DIR, safe_data_path,
    pd, np,
    # table_tools (via core_config)
    multi_table_join, FINANCE_FORMULAS, create_pivot, create_crosstab,
    random_sample, stratified_sample, time_segment_sample, get_file_columns,
    # timeseries_tools (via core_config)
    extract_date_features, resample_timeseries, prophet_forecast,
    rolling_window_calc, decompose_timeseries, adf_test_and_diff, arima_sarima_model,
    # sandbox_exec (via core_config)
    safe_formula_eval, SandboxError,
    # financial_statement (via core_config)
    fetch_financial_data, calculate_ratios, generate_charts, HAS_FIN_STATEMENT,
    # valuation (via core_config)
    dcf_valuation, peer_comparison, HAS_VALUATION,
    # stock_screener (via core_config)
    fetch_all_stocks, screen_stocks, export_csv, HAS_SCREENER,
    # nlp_tools (via core_config)
    segment_and_count, generate_wordcloud, extract_keywords_tfidf, sentiment_analysis,
    lda_topics, text_similarity, HAS_NLP,
)
from auth import local_guard


def _read_csv_auto(path):
    """自适应编码读取 CSV：UTF-8(BOM) → GB18030(GBK超集) → Latin-1（永不失败）"""
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'latin-1'):
        try:
            return pd.read_csv(path, encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return pd.read_csv(path, encoding='latin-1')


def register_routes(app, limiter):
    bp = Blueprint('tools_routes', __name__)
    # 高危面（外部数据拉取/文件读写）：远程请求必须认证
    bp.before_request(local_guard)

    # ==================== Table Operations ====================

    @bp.route("/api/table/join", methods=["POST"])
    def api_table_join():
        try:
            d = request.json
            f1 = str(safe_data_path(d['file1'])) if 'file1' in d else ''
            f2 = str(safe_data_path(d['file2'])) if 'file2' in d else ''
            r = multi_table_join(f1, f2,
                d.get('left_keys'), d.get('right_keys'),
                d.get('join_type', 'inner'),
            )
            return jsonify({"ok": True, **r})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/table/formulas")
    def api_table_formulas():
        return jsonify(list(FINANCE_FORMULAS.keys()))

    @bp.route("/api/table/formula-apply", methods=["POST"])
    def api_table_formula_apply():
        try:
            d = request.json
            fpath = d.get('file')
            if fpath:
                full = str(safe_data_path(fpath))
                df = _read_csv_auto(full)
            else:
                df = pd.read_csv(d.get('data_file'))
            
            # Security: use sandboxed formula evaluator instead of raw eval()
            formula = d.get('formula', '')
            new_col = d.get('new_col', 'result')
            if not formula or not new_col:
                return jsonify({"error": "请提供公式和列名"}), 400
            
            try:
                result = safe_formula_eval(df, formula, new_col)
            except SandboxError as se:
                return jsonify({"error": f"公式被拒绝: {str(se)}"}), 400
            
            # save（仅纯文件名，禁止路径穿越）
            save_name = Path(str(d.get('save_as', 'formula_result.csv'))).name
            if not save_name or '..' in save_name:
                return jsonify({"error": "非法文件名"}), 400
            result.to_csv(str(OUT_DIR / save_name), index=False, encoding='utf-8-sig')
            return jsonify({"ok": True, "rows": len(result), "cols": len(result.columns),
                           "file": save_name, "preview": result.head(10).fillna('').astype(str).to_dict(orient='records')})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/table/pivot", methods=["POST"])
    def api_table_pivot():
        try:
            d = request.json
            fpath = d.get('file')
            if fpath:
                full = str(safe_data_path(fpath))
                df = _read_csv_auto(full)
            else:
                return jsonify({"error": "  "}), 400
            r = create_pivot(df, d.get('index_cols', []), d.get('columns_col', ''),
                            d.get('values_col'), d.get('agg_func', 'mean'), d.get('normalize', False))
            return jsonify({"ok": True, **r})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/table/crosstab", methods=["POST"])
    def api_table_crosstab():
        try:
            d = request.json
            fpath = d.get('file')
            if fpath:
                full = str(safe_data_path(fpath))
                df = _read_csv_auto(full)
            else:
                return jsonify({"error": "  "}), 400
            r = create_crosstab(df, d.get('row_col'), d.get('col_col'),
                               d.get('values_col'), d.get('agg_func', 'count'), d.get('normalize', False))
            return jsonify({"ok": True, **r})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/table/sample", methods=["POST"])
    def api_table_sample():
        try:
            d = request.json
            fpath = d.get('file')
            full = str(safe_data_path(fpath))
            df = _read_csv_auto(full)
            stype = d.get('sample_type', 'random')
            if stype == 'stratified':
                r = stratified_sample(df, d.get('stratify_col'), n=d.get('n'), frac=d.get('frac'))
            elif stype == 'time_segment':
                r = time_segment_sample(df, d.get('n_segments', 10), d.get('sample_per_segment', 5))
            else:
                r = random_sample(df, n=d.get('n'), frac=d.get('frac'))
            return jsonify({"ok": True, **r})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/table/columns", methods=["POST"])
    def api_table_columns():
        try:
            d = request.json
            fpath = d.get('file')
            full = str(safe_data_path(fpath))
            r = get_file_columns(full)
            return jsonify({"ok": True, **r})
        except Exception as e:
            return exc_response(e)

    # ==================== Timeseries Tools ====================

    @bp.route("/api/ts/date-features", methods=["POST"])
    def api_date_features():
        try:
            d = request.json
            full = str(safe_data_path(d['file']))
            df = _read_csv_auto(full)
            r = extract_date_features(df, d.get('date_col', 'date'))
            return jsonify({"ok": True, **r})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/ts/resample", methods=["POST"])
    def api_resample():
        try:
            d = request.json
            full = str(safe_data_path(d['file']))
            df = _read_csv_auto(full)
            r = resample_timeseries(df, d.get('date_col', 'date'), d.get('value_col', 'value'),
                                    d.get('freq', 'D'), d.get('agg_func', 'mean'))
            return jsonify({"ok": True, **r})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/ts/prophet", methods=["POST"])
    def api_prophet():
        try:
            d = request.get_json(silent=True) or {}
            full = str(safe_data_path(d['file']))
            df = _read_csv_auto(full)
            r = prophet_forecast(df, d.get('date_col', 'date'), d.get('value_col', 'value'),
                                 d.get('periods', 30))
            # 依赖缺失/数据不足属客户端可理解的问题 → 400 并带 hint
            if isinstance(r, dict) and r.get('ok') is False:
                return jsonify(r), 400
            return jsonify(r)
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/ts/rolling", methods=["POST"])
    def api_rolling():
        try:
            d = request.json
            full = str(safe_data_path(d['file']))
            df = _read_csv_auto(full)
            r = rolling_window_calc(df, d.get('value_col', 'value'), d.get('window', 20), d.get('calc_types'))
            return jsonify({"ok": True, **r})
        except Exception as e:
            return exc_response(e)

    # -- 时序分解 --
    @bp.route("/api/ts/decompose", methods=["POST"])
    def api_decompose():
        try:
            d = request.json
            full = str(safe_data_path(d['file']))
            df = _read_csv_auto(full)
            r = decompose_timeseries(df, d.get('date_col', 'date'), d.get('value_col', 'value'),
                                     int(d.get('period', 7)), d.get('model', 'additive'))
            return jsonify(r)
        except Exception as e:
            import traceback; traceback.print_exc()
            return exc_response(e)

    # -- ADF 检验 --
    @bp.route("/api/ts/adf", methods=["POST"])
    def api_adf():
        try:
            d = request.json
            full = str(safe_data_path(d['file']))
            df = _read_csv_auto(full)
            r = adf_test_and_diff(df, d.get('value_col', 'value'),
                                  int(d.get('diff_order', 0)),
                                  bool(d.get('auto_diff', False)))
            return jsonify(r)
        except Exception as e:
            import traceback; traceback.print_exc()
            return exc_response(e)

    # -- ARIMA/SARIMA --
    @bp.route("/api/ts/arima", methods=["POST"])
    def api_arima():
        try:
            d = request.json
            full = str(safe_data_path(d['file']))
            df = _read_csv_auto(full)
            order = tuple(d.get('order', [2,1,1]))
            seasonal_order = tuple(d.get('seasonal_order', [1,0,1,7]))
            r = arima_sarima_model(df, d.get('date_col'), d.get('value_col', 'value'),
                                   d.get('model_type', 'auto'), order, seasonal_order,
                                   int(d.get('forecast_periods', 30)))
            return jsonify(r)
        except Exception as e:
            import traceback; traceback.print_exc()
            return exc_response(e)

    # ==================== Hypothesis Testing ====================

    @bp.route("/api/ht/run", methods=["POST"])
    def api_ht_run():
        try:
            import scipy.stats as stats
            import numpy as np
            d = request.json
            full = str(safe_data_path(d['file']))
            df = _read_csv_auto(full)
            test_type = d.get('test_type', 'ttest_ind')
            
            def to_numeric(ser):
                ser = pd.to_numeric(ser, errors='coerce')
                return ser.dropna()
            
            result = {}
            
            if test_type in ('ttest_ind', 'mannwhitneyu'):
                vc = d.get('value_col'); gc = d.get('group_col')
                ga = d.get('group_a'); gb = d.get('group_b')
                if not all([vc, gc, ga, gb]):
                    return jsonify({"error": "请填写数值列、分组列和两个分组值"}), 400
                a = to_numeric(df[df[gc].astype(str) == str(ga)][vc])
                b = to_numeric(df[df[gc].astype(str) == str(gb)][vc])
                if test_type == 'ttest_ind':
                    r = stats.ttest_ind(a, b)
                    result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6),
                              "dof": len(a)+len(b)-2,
                              "sample_stats": {"分组A 均值": float(a.mean()), "分组B 均值": float(b.mean()),
                                                "分组A 样本量": len(a), "分组B 样本量": len(b)}}
                else:
                    r = stats.mannwhitneyu(a, b, alternative='two-sided')
                    result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6),
                              "sample_stats": {"分组A 样本量": len(a), "分组B 样本量": len(b)}}
            
            elif test_type == 'ttest_paired':
                vc1 = d.get('value_col'); vc2 = d.get('value_col2')
                if not all([vc1, vc2]): return jsonify({"error": "请填写两个数值列"}), 400
                a = to_numeric(df[vc1]); b = to_numeric(df[vc2])
                min_len = min(len(a), len(b)); a, b = a[:min_len], b[:min_len]
                r = stats.ttest_rel(a, b)
                result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6),
                          "dof": min_len - 1, "sample_stats": {"配对样本量": min_len,
                          "A 均值": round(float(a.mean()), 2), "B 均值": round(float(b.mean()), 2)}}
            
            elif test_type == 'ttest_1samp':
                vc = d.get('value_col'); pm = d.get('popmean', 0)
                if not vc: return jsonify({"error": "请填写数值列"}), 400
                a = to_numeric(df[vc])
                r = stats.ttest_1samp(a, pm)
                result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6),
                          "dof": len(a)-1, "sample_stats": {"样本量": len(a), "样本均值": round(float(a.mean()), 2),
                          "样本标准差": round(float(a.std(ddof=1)), 2), "假设均值": pm}}
            
            elif test_type == 'anova':
                vc = d.get('value_col'); gc = d.get('group_col')
                if not all([vc, gc]): return jsonify({"error": "请填写数值列和分组列"}), 400
                groups = [to_numeric(g[vc]).values for _, g in df.groupby(gc)]
                r = stats.f_oneway(*groups)
                result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6),
                          "sample_stats": {"分组数": len(groups), "总样本量": sum(len(g) for g in groups)}}
            
            elif test_type == 'chi2':
                vc1 = d.get('value_col'); vc2 = d.get('value_col2')
                if not all([vc1, vc2]): return jsonify({"error": "请填写两个分类列"}), 400
                ct = pd.crosstab(df[vc1], df[vc2])
                r = stats.chi2_contingency(ct)
                result = {"statistic": round(float(r[0]), 4), "p_value": round(float(r[1]), 6),
                          "dof": int(r[2]), "sample_stats": {"列联表形状": f"{ct.shape[0]} x {ct.shape[1]}"}}
            
            elif test_type == 'shapiro':
                vc = d.get('value_col')
                if not vc: return jsonify({"error": "请填写数值列"}), 400
                a = to_numeric(df[vc])
                r = stats.shapiro(a.sample(n=min(5000, len(a)), random_state=42).values)
                result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6),
                          "sample_stats": {"样本量": len(a)}}
            
            elif test_type in ('pearsonr', 'spearmanr'):
                vc1 = d.get('value_col'); vc2 = d.get('value_col2')
                if not all([vc1, vc2]): return jsonify({"error": "请填写两个数值列"}), 400
                a = to_numeric(df[vc1]); b = to_numeric(df[vc2])
                min_len = min(len(a), len(b)); a, b = a[:min_len], b[:min_len]
                fn = stats.pearsonr if test_type == 'pearsonr' else stats.spearmanr
                r = fn(a, b)
                result = {"statistic": round(float(r.statistic), 4),
                          "p_value": round(float(r.pvalue), 6),
                          "correlation": round(float(r.statistic), 4),
                          "sample_stats": {"样本量": min_len}}
            
            elif test_type == 'levene':
                # 方差齐性检验
                vc = d.get('value_col'); gc = d.get('group_col')
                if not all([vc, gc]): return jsonify({"error": "请填写数值列和分组列"}), 400
                groups = [to_numeric(g[vc]).values for _, g in df.groupby(gc)]
                if len(groups) < 2: return jsonify({"error": "至少需要2个分组"}), 400
                r = stats.levene(*groups)
                group_stats = {}
                for i, g in enumerate(groups):
                    group_stats[f"分组{i+1} 方差"] = round(float(np.var(g, ddof=1)), 4)
                    group_stats[f"分组{i+1} 样本量"] = len(g)
                result = {"statistic": round(float(r.statistic), 4), "p_value": round(float(r.pvalue), 6),
                          "sample_stats": {"分组数": len(groups), "总样本量": sum(len(g) for g in groups),
                                            **group_stats}}
            
            elif test_type == 'runs':
                # 游程检验（时序随机性）
                vc = d.get('value_col')
                if not vc: return jsonify({"error": "请填写数值列"}), 400
                a = to_numeric(df[vc]).values
                if len(a) < 10: return jsonify({"error": "数据量至少需要10条"}), 400
                median = np.median(a)
                signs = (a >= median).astype(int)
                runs = 1 + np.sum(np.diff(signs) != 0)
                n1 = np.sum(signs == 1)
                n2 = np.sum(signs == 0)
                # 计算期望游程和标准差
                expected_runs = 1 + 2 * n1 * n2 / (n1 + n2)
                std_runs = np.sqrt(2 * n1 * n2 * (2 * n1 * n2 - n1 - n2) / 
                                  ((n1 + n2)**2 * (n1 + n2 - 1))) if (n1 + n2) > 1 else 1
                z = (runs - expected_runs) / std_runs if std_runs > 0 else 0
                p_val = 2 * stats.norm.sf(abs(z))
                result = {"statistic": round(float(runs), 0), "p_value": round(float(p_val), 6),
                          "z_score": round(float(z), 4),
                          "sample_stats": {"样本量": len(a), "上侧数(n1)": int(n1), "下侧数(n2)": int(n2),
                                            "期望游程": round(float(expected_runs), 2), "中位数": round(float(median), 2)}}
            
            else:
                return jsonify({"error": f"不支持的检验类型: {test_type}"}), 400
            
            return jsonify(result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            return exc_response(e)

    @bp.route("/api/ht/viz", methods=["POST"])
    def api_ht_viz():
        """生成检验可视化图表"""
        try:
            import scipy.stats as stats
            import numpy as np
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import base64, io
            
            d = request.json
            full = str(safe_data_path(d["file"]))
            df = _read_csv_auto(full)
            chart_type = d.get("chart_type", "mean_compare")
            
            fig, ax = plt.subplots(figsize=(8, 5))
            fig.patch.set_facecolor("#ffffff")
            ax.set_facecolor("#f8f9fc")
            ax.grid(True, alpha=0.3, color="#e0e0e0", linestyle='--')
            ax.tick_params(colors="#333333")
            
            if chart_type == "mean_compare":
                vc = d.get("value_col"); gc = d.get("group_col")
                if not vc or not gc: return jsonify({"error": "params"}), 400
                groups = {name: pd.to_numeric(g[vc], errors="coerce").dropna()
                          for name, g in df.groupby(gc)}
                names = list(groups.keys())[:8]
                means = [groups[n].mean() for n in names]
                stds = [groups[n].std() for n in names]
                colors = ["#FF6B6B","#FF8E53","#FFE66D","#4ECDC4","#45B7D1","#FFA07A"][:len(names)]
                bars = ax.bar(range(len(names)), means, yerr=stds, color=colors, alpha=0.8, capsize=5)
                for i, bar in enumerate(bars):
                    ax.text(bar.get_x()+bar.get_width()/2, bar.get_height()+stds[i]+0.5,
                            f"{means[i]:.2f}", ha="center", fontsize=9, color="#333")
                ax.set_xticks(range(len(names)))
                ax.set_xticklabels([str(n)[:12] for n in names], fontsize=9, rotation=30, color="#333")
                ax.set_title(f"分组均值对比 ({vc})", color="#333", fontsize=13, fontweight='600')
            
            elif chart_type == "norm_fit":
                vc = d.get("value_col")
                if not vc: return jsonify({"error": "params"}), 400
                data = pd.to_numeric(df[vc], errors="coerce").dropna().values[:1000]
                ax.hist(data, bins=30, density=True, alpha=0.5, color="#FF6B6B", label="data")
                mu, sigma = float(np.mean(data)), float(np.std(data))
                x_fit = np.linspace(float(data.min()), float(data.max()), 100)
                ax.plot(x_fit, stats.norm.pdf(x_fit, mu, sigma), "-", lw=2, color="#ef5350", label="norm fit")
                s, sp = stats.shapiro(data[:min(5000, len(data))])
                ax.text(0.02, 0.95, f"Shapiro-Wilk: W={s:.4f}, p={sp:.4f}",
                        transform=ax.transAxes, fontsize=9, color="#333",
                        va="top", bbox=dict(boxstyle="round", facecolor="#f8f9fc", alpha=0.8))
                ax.set_title(f"{vc[:15]} 正态分布拟合", color="#333", fontsize=13, fontweight='600')
                ax.legend(fontsize=9, facecolor="#f8f9fc", edgecolor="#ccc", labelcolor="#333")
            
            elif chart_type == "groups_box":
                vc = d.get("value_col"); gc = d.get("group_col")
                if not vc or not gc: return jsonify({"error": "params"}), 400
                groups = [pd.to_numeric(g[vc], errors="coerce").dropna().values for _, g in df.groupby(gc)]
                names = [str(n) for n, _ in df.groupby(gc)]
                bp = ax.boxplot(groups[:8], labels=[n[:12] for n in names[:8]], patch_artist=True)
                colors = ["#4caf50","#2196f3","#ff9800","#e91e63","#9c27b0","#00bcd4"][:len(groups)]
                for patch, color in zip(bp["boxes"], colors):
                    patch.set_facecolor(color); patch.set_alpha(0.6)
                for tick in ax.get_xticklabels():
                    tick.set_color("#333")
                for tick in ax.get_yticklabels():
                    tick.set_color("#333")
                ax.set_title(f"{gc} 分组箱线图", color="#333", fontsize=13, fontweight='600')
            
            plt.tight_layout()
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=100, bbox_inches="tight", facecolor="#ffffff", edgecolor="none")
            buf.seek(0)
            chart = base64.b64encode(buf.read()).decode()
            buf.close(); plt.close()
            return jsonify({"ok": True, "chart": chart, "chart_type": chart_type})
        except Exception as e:
            return exc_response(e)

    # ==================== Financial Statements ====================

    @bp.route("/api/fs/fetch", methods=["POST"])
    def api_fs_fetch():
        """获取财务报表数据"""
        if not HAS_FIN_STATEMENT:
            return jsonify({"error": "financial_statement 模块未加载"}), 500
        data = request.json
        symbol = data.get("symbol", "").strip()
        if not symbol:
            return jsonify({"error": "请输入股票代码"}), 400
        try:
            result = fetch_financial_data(symbol)
            return jsonify(result)
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/fs/ratios", methods=["POST"])
    def api_fs_ratios():
        """计算财务比率"""
        if not HAS_FIN_STATEMENT:
            return jsonify({"error": "financial_statement 模块未加载"}), 500
        data = request.json
        if not data or not data.get("symbol"):
            return jsonify({"error": "请先获取财报数据"}), 400
        try:
            result = calculate_ratios(data)
            return jsonify(result)
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/fs/charts", methods=["POST"])
    def api_fs_charts():
        """生成财务报表分析图表"""
        if not HAS_FIN_STATEMENT:
            return jsonify({"error": "financial_statement 模块未加载"}), 500
        data = request.json
        ratios = data.get("ratios", {})
        try:
            charts = generate_charts({"ratios": ratios})
            return jsonify({"ok": True, "charts": charts})
        except Exception as e:
            return exc_response(e)

    # ==================== Valuation (DCF / 可比公司) ====================

    @bp.route("/api/fs/dcf", methods=["POST"])
    @limiter.limit("5 per minute")
    def api_fs_dcf():
        """绝对估值：现金流折现模型 DCF"""
        if not HAS_VALUATION:
            return jsonify({"error": "valuation 模块未加载"}), 500
        data = request.json or {}
        symbol = str(data.get("symbol", "")).strip()
        if not symbol:
            return jsonify({"error": "请输入股票代码"}), 400
        try:
            result = dcf_valuation(symbol, data.get("params") or {})
            return jsonify(result)
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/fs/compare", methods=["POST"])
    @limiter.limit("5 per minute")
    def api_fs_compare():
        """相对估值：可比公司分析"""
        if not HAS_VALUATION:
            return jsonify({"error": "valuation 模块未加载"}), 500
        data = request.json or {}
        symbol = str(data.get("symbol", "")).strip()
        peers = [p.strip() for p in str(data.get("peers", "")).replace("，", ",").split(",") if p.strip()]
        if not symbol:
            return jsonify({"error": "请输入目标股票代码"}), 400
        try:
            result = peer_comparison(symbol, peers)
            return jsonify(result)
        except Exception as e:
            return exc_response(e)

    # ==================== Stock Screener ====================

    @bp.route("/api/screener/fetch")
    @limiter.limit("5 per minute")
    def api_screener_fetch():
        """获取全量A股数据"""
        if not HAS_SCREENER:
            return jsonify({"error": "stock_screener 模块未加载"}), 500
        try:
            df = fetch_all_stocks()
            if df is None:
                # 返回可诊断的原因（网络不通 / 代理拦截 / 接口变更），而非笼统提示
                try:
                    from stock_screener import LAST_ERROR
                    detail = LAST_ERROR.get("reason") or "东方财富接口未返回数据"
                except Exception:
                    detail = "东方财富接口未返回数据"
                return jsonify({"ok": False,
                                "error": f"数据获取失败：{detail}（请检查网络连通性或代理设置）"}), 500
            cols = ['股票代码', '股票名称', '最新价', '涨跌幅', '市盈率(动态)', '市盈率TTM',
                    '市净率', '净资产收益率', '总市值', '流通市值', '行业', '换手率', '净利润']
            avail = [c for c in cols if c in df.columns]
            df = df[avail].copy()
            # 替换 NaN/Inf 为 None（JSON 兼容）
            import numpy as np
            df = df.replace([np.inf, -np.inf, np.nan], None)
            data = []
            for _, row in df.iterrows():
                clean = {}
                for k, v in row.items():
                    if v is None or (isinstance(v, float) and (pd.isna(v) or np.isinf(v))):
                        clean[k] = None
                    else:
                        clean[k] = v
                data.append(clean)
            return jsonify({"ok": True, "total": len(data), "data": data})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/screener/screen", methods=["POST"])
    def api_screener_screen():
        """按条件筛选"""
        if not HAS_SCREENER:
            return jsonify({"error": "stock_screener 模块未加载"}), 500
        try:
            payload = request.json
            df = pd.DataFrame(payload["data"])
            filters = payload.get("filters", {})
            result = screen_stocks(df, filters)
            display = result.to_dict('records')
            return jsonify({"ok": True, "count": len(display), "display": display})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/screener/export", methods=["POST"])
    def api_screener_export():
        """导出筛选结果为 CSV"""
        if not HAS_SCREENER:
            return jsonify({"error": "stock_screener 模块未加载"}), 500
        try:
            payload = request.json
            df = pd.DataFrame(payload["data"])
            filters = payload.get("filters", {})
            result = screen_stocks(df, filters)
            path = export_csv(result)
            return jsonify({"ok": True, "file": Path(path).name})
        except Exception as e:
            return exc_response(e)

    # ==================== NLP Text Analysis ====================

    @bp.route("/api/nlp/analyze", methods=["POST"])
    def api_nlp_analyze():
        """NLP 分析（支持 file+col 或直接 text）"""
        if not HAS_NLP:
            return jsonify({"error": "nlp_tools 模块未加载"}), 500
        try:
            data = request.json
            texts = None
            if data.get("text"):
                # 直接文本：按行拆分为文档
                texts = [t.strip() for t in str(data["text"]).splitlines() if t.strip()]
                if not texts:
                    texts = [str(data["text"]).strip()]
            else:
                file_path = safe_data_path(data["file"])
                if not file_path.exists():
                    return jsonify({"error": "文件不存在"}), 400

                if file_path.suffix.lower() == ".csv":
                    df = pd.read_csv(file_path, encoding="utf-8")
                elif file_path.suffix.lower() in (".xlsx", ".xls"):
                    df = pd.read_excel(file_path)
                else:
                    return jsonify({"error": "不支持的文件格式"}), 400

                col = data["col"]
                if col not in df.columns:
                    return jsonify({"error": f"找不到列: {col}"}), 400

                texts = df[col].dropna().astype(str).tolist()
            if not texts:
                return jsonify({"error": "文本内容为空"}), 400

            opts = data.get("options", {})
            result = {}

            if opts.get("seg"):
                result["seg"] = segment_and_count(texts)

            if opts.get("sentiment"):
                result["sentiment"] = sentiment_analysis(texts)

            if opts.get("kw"):
                result["keywords"] = extract_keywords_tfidf(texts)

            if opts.get("wc"):
                result["wordcloud"] = generate_wordcloud(texts, bg_color='#0a0a0f',
                    width=800, height=400, max_words=100)

            if opts.get("lda"):
                result["lda"] = lda_topics(texts, n_topics=int(opts.get("lda_topics", 3)))

            if opts.get("similarity"):
                result["similarity"] = text_similarity(
                    texts, target=opts.get("similarity_target") or None)

            result["ok"] = True
            return jsonify(result)

        except Exception as e:
            return exc_response(e)
    app.register_blueprint(bp)
