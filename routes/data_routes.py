"""Data analysis routes — describe, correlation, charts, cleaning, store"""
from core_config import exc_response
from flask import request, jsonify, send_file, Blueprint
from pathlib import Path
import time
from urllib.parse import urlparse
import ipaddress
import socket

from core_config import (
    OUT_DIR, logger, load_dataframe, df_cache, safe_data_path, safe_path_or_resp,
    pd, np, plt,
    # from scraper (via core_config)
    Scraper, load_raw, diagnose, clean, analyze, Storage,
    # from clean_advanced (via core_config)
    get_logger, reset_logger, clean_text_column, encode_categorical,
    standardize_dates, batch_replace, condition_filter, missing_heatmap,
    HAS_CLEAN_ADV,
)
from auth import local_guard
from data_lineage import lineage_mgr


def _assert_public_url(url: str):
    """SSRF 防护：仅允许 http/https 且目标解析为公网地址（拒绝内网/回环/链路本地/元数据）"""
    try:
        parsed = urlparse(url)
    except Exception:
        raise ValueError("URL 无法解析")
    if parsed.scheme not in ("http", "https"):
        raise ValueError("仅允许 http/https URL")
    host = parsed.hostname
    if not host:
        raise ValueError("URL 缺少主机名")
    # 字面 IP 直接校验
    ip = None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None  # 非字面 IP，走域名解析
    if ip is not None:
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"禁止访问内网/保留地址: {host}")
        return
    # 域名 → 解析后逐个 IP 校验
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise ValueError(f"域名无法解析: {host}")
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise ValueError(f"域名解析到内网地址，禁止访问: {host} → {ip}")


def register_routes(app, limiter):
    bp = Blueprint('data_routes', __name__)
    # 高危面（清洗/分析/采集写文件）：远程请求必须认证，本地/LAN 匿名回退 admin
    bp.before_request(local_guard)

    @bp.route("/api/scrape", methods=["POST"])
    def api_scrape():
        data = request.json
        try:
            _assert_public_url(data["url"])
        except ValueError as e:
            return jsonify({"ok": False, "error": f"URL 校验失败: {e}"}), 400
        try:
            scraper = Scraper(
                fields=[f.strip() for f in data["fields"].split(",") if f.strip()],
            )
            rows = scraper.run(
                url=data["url"],
                item_selector=data["selector"],
                max_pages=data.get("pages", 1),
                detail=data.get("detail", False),
                next_selector=data.get("next") or None,
            )
            if rows:
                df = pd.DataFrame(rows)
                df.to_csv(OUT_DIR / "raw.csv", index=False, encoding="utf-8-sig")
                try:
                    lineage_mgr.record("scrape", data["url"], "raw.csv",
                                       detail=f"{len(rows)}行")
                except Exception:
                    pass

            return jsonify({"ok": True, "stats": scraper.stats})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/clean", methods=["POST"])
    @limiter.limit("10 per minute")
    def api_clean():
        data = request.json
        try:
            path = safe_data_path(data["file"])
            df = load_raw(str(path))
            diag = diagnose(df)
            df = clean(
                df,
                drop_dupes=not data.get("keep_dupes", False),
                fill_mode=data.get("fill", "none"),
                outlier_std=data.get("outlier", 0),
            )
            out_path = OUT_DIR / "clean.csv"
            df.to_csv(out_path, index=False, encoding="utf-8-sig")
            # Build detailed missing info per column
            missing_detail = []
            total_rows = diag.get("_rows_before", len(df) + diag.get("duplicates", 0))
            try:
                lineage_mgr.record("clean", data["file"], "clean.csv",
                                   detail=f"{total_rows}行 → {len(df)}行")
            except Exception:
                pass
            for col_name, miss_count in diag["missing"].items():
                pct = round(miss_count / total_rows * 100, 1)
                missing_detail.append({"col": col_name, "count": miss_count, "pct": pct})

            return jsonify({
                "ok": True,
                "diag": {
                    "duplicates": diag["duplicates"],
                    "missing_cols": ", ".join(diag["missing"].keys()) if diag["missing"] else "",
                    "missing_detail": missing_detail,
                    "total_missing": sum(diag["missing"].values()) if diag["missing"] else 0,
                },
                "rows_before": total_rows,
                "rows_after": len(df),
                "path": str(out_path.name),
            })
        except Exception as e:
            return exc_response(e)

    # ==================== 数据清洗增强 API ====================

    @bp.route("/api/clean/init", methods=["POST"])
    def api_clean_init():
        """初始化清洗日志（创建快照）"""
        if not HAS_CLEAN_ADV:
            return jsonify({"error": "clean_advanced 模块未加载"}), 500
        try:
            data = request.json
            filepath = str(safe_data_path(data["file"]))
            reset_logger(filepath)
            log = get_logger(filepath).get_log()
            return jsonify({"ok": True, "log": log, "rows": len(get_logger(filepath).get_df())})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/clean/text", methods=["POST"])
    def api_clean_text():
        """文本清洗"""
        if not HAS_CLEAN_ADV:
            return jsonify({"error": "clean_advanced 模块未加载"}), 500
        try:
            data = request.json
            filepath = str(safe_data_path(data["file"]))
            cl = get_logger(filepath)
            df, result = clean_text_column(
                cl.get_df(), data["column"],
                to_lower=data.get("to_lower", True),
                to_upper=data.get("to_upper", False),
                remove_special=data.get("remove_special", True),
                trim_spaces=data.get("trim_spaces", True),
                tokenize=data.get("tokenize", False),
            )
            cl.commit("文本清洗: " + data["column"], df)
            return jsonify({"ok": True, **result})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/clean/encode", methods=["POST"])
    def api_clean_encode():
        """分类变量编码"""
        if not HAS_CLEAN_ADV:
            return jsonify({"error": "clean_advanced 模块未加载"}), 500
        try:
            data = request.json
            filepath = str(safe_data_path(data["file"]))
            cl = get_logger(filepath)
            df, result = encode_categorical(
                cl.get_df(), data["column"],
                method=data.get("method", "onehot"),
                drop_first=data.get("drop_first", False))
            cl.commit("编码({}): {}".format(data.get("method"), data["column"]), df)
            return jsonify({"ok": True, **result})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/clean/date", methods=["POST"])
    def api_clean_date():
        """日期标准化"""
        if not HAS_CLEAN_ADV:
            return jsonify({"error": "clean_advanced 模块未加载"}), 500
        try:
            data = request.json
            filepath = str(safe_data_path(data["file"]))
            cl = get_logger(filepath)
            df, result = standardize_dates(
                cl.get_df(), data["column"],
                target_format=data.get("format", "%Y-%m-%d"),
                remove_invalid=data.get("remove_invalid", True))
            cl.commit("日期标准化: " + data["column"], df)
            return jsonify({"ok": True, **result})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/clean/replace", methods=["POST"])
    def api_clean_replace():
        """批量替换"""
        if not HAS_CLEAN_ADV:
            return jsonify({"error": "clean_advanced 模块未加载"}), 500
        try:
            data = request.json
            filepath = str(safe_data_path(data["file"]))
            cl = get_logger(filepath)
            df, result = batch_replace(
                cl.get_df(), data["column"], str(data.get("old_value", "")),
                str(data.get("new_value", "")), data.get("regex", False))
            cl.commit("替换: " + data["column"], df)
            return jsonify({"ok": True, **result})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/clean/filter", methods=["POST"])
    def api_clean_filter():
        """条件过滤删除"""
        if not HAS_CLEAN_ADV:
            return jsonify({"error": "clean_advanced 模块未加载"}), 500
        try:
            data = request.json
            filepath = str(safe_data_path(data["file"]))
            cl = get_logger(filepath)
            df, result = condition_filter(
                cl.get_df(), data["column"], data["operator"], data.get("value", ""))
            cl.commit("过滤: {} {} {}".format(data["column"], data["operator"], data.get("value", "")), df)
            return jsonify({"ok": True, **result})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/clean/undo", methods=["POST"])
    def api_clean_undo():
        """撤销上一步清洗"""
        if not HAS_CLEAN_ADV:
            return jsonify({"error": "clean_advanced 模块未加载"}), 500
        try:
            data = request.json
            filepath = str(safe_data_path(data["file"]))
            cl = get_logger(filepath)
            ok, msg = cl.undo()
            log = cl.get_log()
            return jsonify({"ok": ok, "message": msg, "log": log, "rows": len(cl.get_df())})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/clean/save", methods=["POST"])
    @limiter.limit("20 per minute")
    def api_clean_save():
        """保存清洗结果"""
        if not HAS_CLEAN_ADV:
            return jsonify({"error": "clean_advanced 模块未加载"}), 500
        try:
            data = request.json
            filepath = str(safe_data_path(data["file"]))
            out_name = data.get("out_name", "clean_advanced.csv")
            # 仅接受纯文件名，禁止路径分隔符与 ..
            out_name = Path(str(out_name)).name
            if not out_name or '..' in out_name or '/' in out_name or '\\' in out_name:
                return jsonify({"error": "非法文件名"}), 400
            out_path = OUT_DIR / out_name
            cl = get_logger(filepath)
            cl.save(str(out_path))
            try:
                lineage_mgr.record("clean_save", data["file"], out_name,
                                   detail=f"{len(cl.get_df())}行")
            except Exception:
                pass
            return jsonify({"ok": True, "path": out_name, "rows": len(cl.get_df())})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/clean/missing-heatmap", methods=["POST"])
    def api_clean_missing_heatmap():
        """缺失值热力图"""
        if not HAS_CLEAN_ADV:
            return jsonify({"error": "clean_advanced 模块未加载"}), 500
        try:
            data = request.json
            filepath = str(safe_data_path(data["file"]))
            info, chart = missing_heatmap(filepath)
            return jsonify({"ok": True, **info, "chart": chart})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/clean/log", methods=["POST"])
    def api_clean_log():
        """获取清洗日志"""
        if not HAS_CLEAN_ADV:
            return jsonify({"log": [], "rows": 0})
        try:
            data = request.json
            filepath = str(safe_data_path(data["file"]))
            cl = get_logger(filepath)
            return jsonify({"ok": True, "log": cl.get_log(), "rows": len(cl.get_df())})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/analyze", methods=["POST"])
    def api_analyze():
        data = request.json
        try:
            path = safe_data_path(data["file"])
            df = load_dataframe(str(path))

            num_cols = df.select_dtypes(include=np.number).columns

            # stats  HTML table
            stats_html = ""
            if len(num_cols):
                desc = df[num_cols].describe().T
                desc["median"] = df[num_cols].median()
                desc["skew"] = df[num_cols].skew()
                stats_html = "<table><tr><th></th>" + "".join(f"<th>{c}</th>" for c in desc.columns) + "</tr>"
                for idx, row in desc.iterrows():
                    stats_html += f"<tr><td>{idx}</td>" + "".join(
                        f"<td>{row[c]:.4g}" if isinstance(row[c], float) else f"<td>{row[c]}</td>"
                        for c in desc.columns
                    ) + "</tr>"
                stats_html += "</table>"

            # charts
            group_col = data.get("group", "").strip() or None
            analyze(df, group_col=group_col, charts=True)

            chart_dir = OUT_DIR / "charts"
            charts = []
            for img in sorted(chart_dir.glob("*.png")) if chart_dir.exists() else []:
                charts.append({"name": img.name, "path": f"charts/{img.name}"})

            # Auto-generate conclusion
            conclusion = []
            if len(num_cols):
                # Find high skew columns
                skew_vals = df[num_cols].skew()
                high_skew = [(col, round(sk, 2)) for col, sk in skew_vals.items() if abs(sk) > 1]
                if high_skew:
                    conclusion.append("偏态分布: " + ", ".join(f"{col}(skew={sk})" for col, sk in high_skew) + "，建议对数变换")
                else:
                    conclusion.append("数值字段分布较对称，无明显偏态")

                # Find high missing rate
                miss_rate = df[num_cols].isnull().mean()
                high_miss = [(col, round(r*100, 1)) for col, r in miss_rate.items() if r > 0.05]
                if high_miss:
                    conclusion.append("缺失较多: " + ", ".join(f"{col}({pct}%)" for col, pct in high_miss))

                # Correlation insight
                if len(num_cols) >= 2:
                    corr = df[num_cols].corr().abs()
                    # Find strongest correlation pair (excluding self)
                    max_corr = 0
                    max_pair = ("", "")
                    for i in range(len(corr.columns)):
                        for j in range(i+1, len(corr.columns)):
                            v = corr.iloc[i, j]
                            if v > max_corr and not pd.isna(v):
                                max_corr = round(v, 3)
                                max_pair = (corr.columns[i], corr.columns[j])
                    if max_pair[0] and max_corr > 0.3:
                        conclusion.append(f"最强相关: {max_pair[0]} & {max_pair[1]} (r={max_corr})")
                    elif max_pair[0]:
                        conclusion.append("字段间相关性较弱，各维度较为独立")

                # Outlier hint
                for col in list(num_cols)[:8]:
                    q1, q3 = df[col].quantile(0.25), df[col].quantile(0.75)
                    iqr = q3 - q1
                    if iqr > 0:
                        outliers = ((df[col] < q1 - 1.5*iqr) | (df[col] > q3 + 1.5*iqr)).sum()
                        if outliers > len(df) * 0.05:
                            conclusion.append(f"异常值较多: {col} ({outliers}个)")
            else:
                conclusion.append("无数值字段可分析")

            return jsonify({"ok": True, "stats_html": stats_html, "charts": charts, "conclusion": conclusion})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/chart/analyze", methods=["POST"])
    def api_chart_analyze():
        """智能图表推荐 + 自动洞察（chart_insight_engine 接线）"""
        data = request.json or {}
        try:
            fname = (data.get("file") or "").strip()
            if not fname:
                return jsonify({"ok": False, "error": "缺少 file 参数"}), 400
            path = safe_path_or_resp(fname)
            if isinstance(path, tuple):
                return path
            if not path.exists():
                return jsonify({"ok": False, "error": f"文件不存在: {fname}"}), 404
            df = load_dataframe(str(path)).head(2000)
            records = df.fillna("").to_dict(orient="records")
            columns = df.columns.tolist()
            try:
                from chart_insight_engine import chart_insight_engine
                result = chart_insight_engine.analyze(
                    records, columns, query=(data.get("column") or ""))
                return jsonify({"ok": True, **result})
            except ImportError:
                return jsonify({"ok": False, "error": "chart_insight_engine 模块不可用"}), 500
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/chart/<path:name>")
    def api_chart(name):
        try:
            p = safe_data_path(name)
        except PermissionError:
            return "forbidden", 403
        if p.exists() and p.is_file():
            return send_file(str(p), mimetype="image/png")
        return "not found", 404

    @bp.route("/api/describe", methods=["POST"])
    def api_describe():
        """描述性统计"""
        data = request.json
        try:
            fname = (data.get("file") or "").strip()
            if not fname:
                return jsonify({"error": "未选择数据文件，请先在数据分析页加载文件"}), 400
            path = safe_data_path(fname)
            if not path.exists():
                return jsonify({"error": f"文件不存在: {fname}"}), 400
            df = load_dataframe(str(path))
            num_cols = df.select_dtypes(include=[np.number]).columns

            stats_html = ""
            if len(num_cols):
                desc = df[num_cols].describe().T
                desc["median"] = df[num_cols].median()
                desc["skew"] = df[num_cols].skew()
                desc["kurtosis"] = df[num_cols].kurtosis()
                stats_html = "<table><tr><th></th>" + "".join(f"<th>{c}</th>" for c in desc.columns) + "</tr>"
                for idx, row in desc.iterrows():
                    stats_html += f"<tr><td>{idx}</td>" + "".join(
                        f"<td>{row[c]:.4g}</td>" if isinstance(row[c], (int, float)) else f"<td>{row[c]}</td>"
                        for c in desc.columns
                    ) + "</tr>"
                stats_html += "</table>"

            # 自动生成分析结论
            conclusion = []

            # 偏度分析
            skew_vals = df[num_cols].skew()
            high_skew = [(col, round(sk, 2)) for col, sk in skew_vals.items() if abs(sk) > 1]
            if high_skew:
                conclusion.append("<b>📐 偏态分布：</b>" + ", ".join(f"{col}(skew={sk})" for col, sk in high_skew) + "，建议做对数变换")
            else:
                conclusion.append("<b>📐 分布形态：</b>各变量分布较对称，无明显偏态")

            # 缺失值分析
            miss_rate = df[num_cols].isnull().mean()
            high_miss = [(col, round(r*100, 1)) for col, r in miss_rate.items() if r > 0.05]
            if high_miss:
                conclusion.append("<b>⚠️ 缺失较多：</b>" + ", ".join(f"{col}({pct}%)" for col, pct in high_miss))
            else:
                conclusion.append("<b>✅ 数据完整：</b>各变量缺失率均低于5%")

            # 相关性最强的变量对
            if len(num_cols) >= 2:
                corr = df[num_cols].corr().abs()
                max_corr = 0
                max_pair = ("", "")
                for i in range(len(corr.columns)):
                    for j in range(i+1, len(corr.columns)):
                        v = corr.iloc[i, j]
                        if v > max_corr and not pd.isna(v):
                            max_corr = round(v, 3)
                            max_pair = (corr.columns[i], corr.columns[j])
                if max_pair[0] and max_corr > 0.3:
                    conclusion.append(f"<b>🔗 最强相关：</b>{max_pair[0]} & {max_pair[1]} (r={max_corr})")

            # 异常值提示
            outlier_cols = []
            for col in list(num_cols)[:8]:
                q1, q3 = df[col].quantile(0.25), df[col].quantile(0.75)
                iqr = q3 - q1
                if iqr > 0:
                    outliers = ((df[col] < q1 - 1.5*iqr) | (df[col] > q3 + 1.5*iqr)).sum()
                    if outliers > len(df) * 0.05:
                        outlier_cols.append(f"{col}({outliers}个)")
            if outlier_cols:
                conclusion.append("<b>🔺 异常值：</b>" + ", ".join(outlier_cols))

            stats_html += '<div style="margin-top:16px;padding:12px;background:rgba(94,106,210,0.06);border-radius:8px;"><div style="font-weight:600;margin-bottom:8px;color:#FF6B6B;">📋 分析结论</div>' \
                + "".join(f'<div style="font-size:13px;line-height:1.8;color:#555;">{c}</div>' for c in conclusion) + '</div>'

            return jsonify({"ok": True, "stats_html": stats_html, "conclusion": conclusion})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/correlation", methods=["POST"])
    def api_correlation():
        """相关性分析"""
        data = request.json
        try:
            fname = (data.get("file") or "").strip()
            if not fname:
                return jsonify({"error": "未选择数据文件，请先在数据分析页加载文件"}), 400
            path = safe_data_path(fname)
            if not path.exists():
                return jsonify({"error": f"文件不存在: {fname}"}), 400
            df = load_dataframe(str(path))

            num_cols = df.select_dtypes(include=[np.number]).columns

            corr_html = ""
            if len(num_cols) >= 2:
                corr = df[num_cols].corr()
                corr_html = "<table><tr><th></th>" + "".join(f"<th>{c}</th>" for c in corr.columns) + "</tr>"
                for idx, row in corr.iterrows():
                    corr_html += f"<tr><td>{idx}</td>"
                    for c in corr.columns:
                        val = row[c]
                        if abs(val) > 0.7 and idx != c:
                            corr_html += f"<td style='background:rgba(94,106,210,0.3);font-weight:600;'>{val:.3f}</td>"
                        else:
                            corr_html += f"<td>{val:.3f}</td>"
                    corr_html += "</tr>"
                corr_html += "</table>"

                pairs = []
                for i in range(len(corr.columns)):
                    for j in range(i+1, len(corr.columns)):
                        v = corr.iloc[i, j]
                        if abs(v) > 0.5:
                            pairs.append((corr.columns[i], corr.columns[j], round(v, 3)))
                pairs.sort(key=lambda x: abs(x[2]), reverse=True)

                if pairs:
                    corr_html += "<div style='margin-top:12px;'><b>强相关对 (|r|>0.5):</b><ul>"
                    for a, b, v in pairs[:10]:
                        corr_html += f"<li>{a} & {b}: r={v}</li>"
                    corr_html += "</ul></div>"

            return jsonify({"ok": True, "corr_html": corr_html})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/group", methods=["POST"])
    def api_group():
        """数据分组统计"""
        data = request.json
        try:
            path = safe_data_path(data["file"])
            group_col = data.get("group", "")
            if not group_col:
                return jsonify({"error": "请选择分组列"}), 400

            suffix = path.suffix.lower()
            df = load_dataframe(str(path))

            if group_col not in df.columns:
                return jsonify({"error": f"列 '{group_col}' 不存在"}), 400

            num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
            if not num_cols:
                return jsonify({"error": "没有数值列可供分组统计"}), 400

            grouped = df.groupby(group_col)[num_cols]
            stats = grouped.agg(['mean', 'median', 'std', 'count'])

            html = '<div style="overflow-x:auto;"><table>'
            html += f'<tr><th>{group_col}</th>'
            for col in num_cols[:6]:
                html += f'<th colspan="3" style="text-align:center;border-bottom:2px solid #FF6B6B;">{col}</th>'
            html += '</tr><tr><th></th>'
            for col in num_cols[:6]:
                html += '<th style="font-size:11px;color:#888;">均值</th><th style="font-size:11px;color:#888;">中位数</th><th style="font-size:11px;color:#888;">标准差</th>'
            html += '</tr>'

            for idx, row in stats.iterrows():
                html += f'<tr><td style="font-weight:600;">{idx}</td>'
                for col in num_cols[:6]:
                    mean_val = row.get((col, 'mean'), 0)
                    median_val = row.get((col, 'median'), 0)
                    std_val = row.get((col, 'std'), 0)
                    html += f'<td>{mean_val:.3f}</td><td>{median_val:.3f}</td><td>{std_val:.3f}</td>'
                html += '</tr>'
            html += '</table></div>'

            counts = df[group_col].value_counts().sort_index()
            html += '<div style="margin-top:12px;font-size:12px;color:var(--ink-subtle);"><b>各组样本量：</b>'
            html += ', '.join(f'{k}: {v}条' for k, v in counts.items())
            html += '</div>'

            return jsonify({"ok": True, "html": html})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/chart/custom", methods=["POST"])
    @limiter.limit("30 per minute")
    def api_custom_chart():
        """自定义绘图（支持多列）MULTI_COLUMN_V2"""
        data = request.json
        try:
            path = safe_data_path(data["file"])
            x_cols = data.get("x", [])
            y_cols = data.get("y", [])
            chart_type = data.get("type", "scatter")

            # 统一转为列表
            if isinstance(x_cols, str): x_cols = [x_cols]
            if isinstance(y_cols, str): y_cols = [y_cols]
            x_cols = [c for c in x_cols if c]
            y_cols = [c for c in y_cols if c]

            if not x_cols and chart_type != "hist":
                return jsonify({"error": "请选择 X 轴列"}), 400

            df = load_dataframe(str(path))

            for col in x_cols + y_cols:
                if col not in df.columns:
                    return jsonify({"error": f"列 '{col}' 不存在"}), 400

            plot_cols = list(dict.fromkeys(x_cols + y_cols))  # 保持顺序去重
            plot_df = df[plot_cols].dropna()

            n_x, n_y = len(x_cols), len(y_cols)

            # 单X时所有Y画在一张图；多X时用子图
            if n_x == 1:
                fig, ax = plt.subplots(figsize=(10, 6))
                axes = [ax]
            else:
                fig, axes = plt.subplots(n_x, 1, figsize=(10, 5 * n_x))
                if n_x == 1: axes = [axes]

            fig.patch.set_facecolor('#ffffff')

            colors = ['#FF6B6B', '#FF8E53', '#FFE66D', '#4ECDC4', '#45B7D1', '#FFA07A', '#FFD700', '#87CEEB']

            for idx, ax in enumerate(axes):
                ax.set_facecolor('#f8f9fc')
                ax.grid(True, alpha=0.3, color='#e0e0e0', linestyle='--')
                ax.tick_params(colors='#333333')
                ax.spines['bottom'].set_color('#cccccc')
                ax.spines['left'].set_color('#cccccc')
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)

                xc = x_cols[min(idx, n_x - 1)] if n_x > 0 else None

                if chart_type == 'line' and n_y > 0:
                    # 按X聚合均值，统一用数字位置画图
                    for i, yc in enumerate(y_cols):
                        ps = plot_df.groupby(xc)[yc].mean().sort_index()
                        x_pos = list(range(len(ps)))
                        ax.plot(x_pos, ps.values, color=colors[i % len(colors)], linewidth=2, label=yc, marker='o', markersize=4)
                    from chart_helpers import calc_label_params
                    lp = calc_label_params(list(ps.index))
                    ax.set_xticks(range(len(ps)))
                    ax.set_xticklabels([str(v) for v in ps.index],
                                      rotation=lp["rotation"], ha=lp["ha"],
                                      fontsize=lp["fontsize"], color='#333')
                    ax.set_xlabel(xc, fontsize=10, color='#333')
                    ax.set_ylabel('均值', fontsize=10, color='#333')
                    ax.set_title(f'折线图 ({xc})', fontsize=12, color='#333', fontweight='600')
                    ax.legend(fontsize=9, facecolor='#ffffff', edgecolor='#cccccc', labelcolor='#333')
                elif chart_type == 'scatter' and n_y > 0:
                    yc = y_cols[0]
                    ax.scatter(plot_df[xc], plot_df[yc], alpha=0.6, s=40, c='#FF6B6B', edgecolors='white')
                    ax.set_xlabel(xc, fontsize=10, color='#333')
                    ax.set_ylabel(yc, fontsize=10, color='#333')
                    ax.set_title(f'{xc} vs {yc}', fontsize=12, color='#333', fontweight='600')
                elif chart_type == 'bar' and n_y > 0:
                    yc = y_cols[0]
                    bd = plot_df.groupby(xc)[yc].mean().sort_index()
                    from chart_helpers import calc_label_params
                    lp = calc_label_params(list(bd.index))
                    if lp["horizontal"]:
                        ax.barh(range(len(bd)), bd.values, color='#FF6B6B')
                        ax.set_yticks(range(len(bd)))
                        ax.set_yticklabels(bd.index, fontsize=lp["fontsize"], color='#333')
                        ax.set_ylabel(xc, fontsize=10, color='#333')
                        ax.set_xlabel(yc + ' (均值)', fontsize=10, color='#333')
                    else:
                        ax.bar(range(len(bd)), bd.values, color='#FF6B6B')
                        ax.set_xticks(range(len(bd)))
                        ax.set_xticklabels(bd.index, rotation=lp["rotation"], ha=lp["ha"],
                                          fontsize=lp["fontsize"], color='#333')
                        ax.set_xlabel(xc, fontsize=10, color='#333')
                        ax.set_ylabel(yc + ' (均值)', fontsize=10, color='#333')
                    ax.set_title(f'{xc} vs {yc}', fontsize=12, color='#333', fontweight='600')
                elif chart_type == 'hist' and xc:
                    ax.hist(plot_df[xc], bins=30, color='#FF6B6B', edgecolor='white', alpha=0.8)
                    ax.set_xlabel(xc, fontsize=10, color='#333')
                    ax.set_ylabel('频数', fontsize=10, color='#333')
                    ax.set_title(f'{xc} 分布直方图', fontsize=12, color='#333', fontweight='600')
                elif chart_type == 'box' and n_y > 0:
                    yc = y_cols[0]
                    groups = plot_df.groupby(xc)[yc].apply(list).to_dict()
                    bp = ax.boxplot(groups.values(), tick_labels=list(groups.keys()),
                                   patch_artist=True, boxprops=dict(facecolor='#FF6B6B', alpha=0.6))
                    ax.set_xlabel(xc, fontsize=10, color='#333')
                    ax.set_ylabel(yc, fontsize=10, color='#333')
                    ax.set_title(f'{xc} vs {yc} 箱线图', fontsize=12, color='#333', fontweight='600')

            plt.tight_layout()
            img_name = f"custom_{int(time.time()*1000)}.png"
            chart_dir = OUT_DIR / "charts"
            chart_dir.mkdir(exist_ok=True)
            fig.savefig(str(chart_dir / img_name), dpi=100, bbox_inches='tight', facecolor='#ffffff', edgecolor='none')
            plt.close()

            stats_text = f"样本量: {len(plot_df)}"
            for xc in x_cols[:3]:
                for yc in y_cols[:3]:
                    if yc in plot_df.columns:
                        try:
                            corr = plot_df[xc].corr(plot_df[yc])
                            stats_text += f" | {xc}&{yc}: r={corr:.3f}"
                        except Exception:
                            pass

            return jsonify({"ok": True, "img": img_name, "stats": stats_text})
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/chart/echarts-data", methods=["POST"])
    @limiter.limit("20 per minute")
    def api_chart_echarts_data():
        """返回 echarts 交互渲染所需数据（前端可缩放/平移/悬停）"""
        data = request.json or {}
        file = (data.get("file") or "").strip()
        x = (data.get("x") or "").strip()
        y_cols = data.get("y") or []
        limit = min(int(data.get("limit", 500)), 2000)
        if not file:
            return jsonify({"error": "缺少 file"}), 400
        if isinstance(y_cols, str):
            y_cols = [y_cols]
        y_cols = [c for c in y_cols if c]
        if not y_cols:
            return jsonify({"error": "请选择至少一个 Y 列"}), 400
        path = OUT_DIR / file
        if not path.exists():
            return jsonify({"error": "文件不存在"}), 404
        df = load_dataframe(str(path))
        df = df.head(limit)
        for c in y_cols:
            if c not in df.columns:
                return jsonify({"error": f"列 '{c}' 不存在"}), 400
            if not pd.api.types.is_numeric_dtype(df[c]):
                try:
                    df[c] = pd.to_numeric(df[c], errors='coerce')
                except Exception:
                    pass
        x_vals = []
        if x and x in df.columns:
            x_vals = df[x].astype(str).tolist()
        elif 'date' in df.columns:
            x_vals = df['date'].astype(str).tolist()
        else:
            x_vals = [str(i) for i in range(len(df))]
        series = []
        for c in y_cols:
            vals = []
            for v in df[c].tolist():
                try:
                    fv = float(v)
                    vals.append(None if pd.isna(fv) else round(fv, 4))
                except (TypeError, ValueError):
                    vals.append(None)
            series.append({"name": c, "data": vals})
        return jsonify({"ok": True, "x": x_vals, "series": series, "n": len(df)})

    @bp.route("/api/chart/plotly", methods=["POST"])
    @limiter.limit("20 per minute")
    def api_chart_plotly():
        """交互式 Plotly 图表（返回 HTML 片段）"""
        data = request.json
        try:
            path = safe_data_path(data["file"])
            x_cols = data.get("x", [])
            y_cols = data.get("y", [])
            chart_type = data.get("type", "scatter")

            if isinstance(x_cols, str): x_cols = [x_cols]
            if isinstance(y_cols, str): y_cols = [y_cols]
            x_cols = [c for c in x_cols if c]
            y_cols = [c for c in y_cols if c]

            df = load_dataframe(str(path))
            for col in x_cols + y_cols:
                if col not in df.columns:
                    return jsonify({"error": f"列 '{col}' 不存在"}), 400

            plot_cols = list(dict.fromkeys(x_cols + y_cols))
            plot_df = df[plot_cols].dropna()

            from chart_helpers import build_plotly_chart
            html = build_plotly_chart(plot_df, x_cols, y_cols, chart_type)
            return html
        except Exception as e:
            return exc_response(e)

    @bp.route("/api/store", methods=["POST"])
    def api_store():
        data = request.json
        try:
            path = safe_data_path(data["file"])
            df = load_dataframe(str(path))
            unique = [u.strip() for u in data.get("unique", "").split(",") if u.strip()] or None
            st = Storage()
            n = st.save(df, table=data.get("table", "scraped"), unique_cols=unique)
            st.export_csv(table=data.get("table", "scraped"), name=data.get("export_name", "export"))
            st.close()
            return jsonify({"ok": True, "inserted": n, "csv_name": data.get("export_name", "export")})
        except Exception as e:
            return exc_response(e)
    app.register_blueprint(bp)
