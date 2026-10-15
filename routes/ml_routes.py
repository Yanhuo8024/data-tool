"""ML routes — model training, prediction, feature engineering"""
from core_config import exc_response, safe_path_or_resp
import pickle, json
from pathlib import Path
from datetime import datetime
from flask import request, jsonify, Blueprint

from core_config import (
    OUT_DIR, logger, load_dataframe,
    # 注意: MLTrainer / get_ml_model_list / HAS_ML_ENHANCED / ALGORITHM_CATALOG
    # 不再顶层 import — 它们触发 ml_module (sklearn 全套) 立即加载，是冷启动最大瓶颈。
    # 改为在 view function 内 lazy import（首次访问 /api/ml/* 时才真正加载 sklearn）
    pd, np, os, time, traceback, json_safe,
    _get_csv_file, safe_load,
)
from auth import local_guard
from data_lineage import lineage_mgr


# -- conditionally import feature_engineering (used by /api/feature/*) --
try:
    from feature_engineering import (
        auto_feature_pipeline,
        add_lag_features, add_rolling_features, add_growth_features,
        add_diff_features, add_date_features,
        add_cross_cat_features, add_cross_num_features, add_cross_stat_features,
    )
    HAS_FEATURE_ENGINEERING = True
except ImportError:
    HAS_FEATURE_ENGINEERING = False


def register_routes(app, limiter):
    bp = Blueprint('ml_routes', __name__)
    # 高危面（训练/预测/模型删除）：远程请求必须认证，本地/LAN 匿名回退 admin
    bp.before_request(local_guard)
    """Register all /api/ml/* and /api/feature/* routes on the Flask app."""

    # ============================================================
    #  /api/ml/models  (GET)
    # ============================================================
    @bp.route("/api/ml/models")
    def ml_models():
        """获取可用的ML模型列表"""
        from core_config import ml_model_list
        return jsonify(ml_model_list())

    # ============================================================
    #  /api/ml/columns  (GET)
    # ============================================================
    @bp.route("/api/ml/columns")
    def ml_columns():
        """获取可用于ML的列信息"""
        try:
            file_name = request.args.get("file")
            if file_name:
                # P0 修复：路径穿越守卫（../../ 可读 OUT_DIR 外文件）
                _p = safe_path_or_resp(file_name, error_msg="非法文件路径")
                if isinstance(_p, tuple):
                    return _p
                filepath = str(_p)
                if not os.path.exists(filepath):
                    return jsonify({"error": "文件不存在"}), 400
            else:
                # 优先找 xlsx，再找 csv
                files = [f for f in os.listdir(OUT_DIR) if f.endswith(('.csv', '.xlsx', '.xls'))]
                if not files:
                    return jsonify({"error": "没有可用的数据文件"}), 400
                # 按修改时间排序，取最新
                files.sort(key=lambda f: os.path.getmtime(os.path.join(OUT_DIR, f)), reverse=True)
                filepath = os.path.join(OUT_DIR, files[0])
                file_name = files[0]

            # 根据扩展名选择读取方式
            suffix = os.path.splitext(filepath)[1].lower()
            if suffix in ('.xlsx', '.xls'):
                df = pd.read_excel(filepath)
            else:
                # 尝试多种编码
                for enc in ['utf-8-sig', 'utf-8', 'gbk', 'gb2312', 'gb18030']:
                    try:
                        df = pd.read_csv(filepath, encoding=enc)
                        break
                    except (UnicodeDecodeError, UnicodeError):
                        continue
                else:
                    return jsonify({"error": "文件编码无法识别"}), 400

            numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
            categorical_cols = df.select_dtypes(include=['object', 'category']).columns.tolist()
            all_cols = df.columns.tolist()

            return jsonify({
                "file": file_name,
                "columns": all_cols,
                "numeric": numeric_cols,
                "categorical": categorical_cols,
                "rows": len(df),
            })
        except Exception as e:
            return exc_response(e)

    # ============================================================
    #  /api/ml/train  (POST) – rate-limited
    # ============================================================
    @bp.route("/api/ml/train", methods=["POST"])
    @limiter.limit("10 per minute")
    def ml_train():
        """训练机器学习模型"""
        try:
            data = request.get_json() or {}
            task_type = data.get('task_type', 'regression')
            model_name = data.get('model_name', 'linear_regression')
            feature_cols = data.get('feature_cols', [])
            target_col = data.get('target_col')
            n_clusters = data.get('n_clusters', 3)
            test_size = data.get('test_size', 0.2)
            tune_hyperparams = data.get('tune_hyperparams', False)
            file_name = data.get('file_name')

            if not feature_cols:
                return jsonify({"error": "请选择特征列"}), 400

            # 读取数据
            if file_name:
                # P0 修复：路径穿越守卫
                _p = safe_path_or_resp(file_name, error_msg="非法文件路径")
                if isinstance(_p, tuple):
                    return _p
                file_path = str(_p)
            else:
                files = [f for f in os.listdir(OUT_DIR) if f.endswith('.csv')]
                if not files:
                    return jsonify({"error": "没有可用的CSV文件"}), 400
                latest = max(files, key=lambda f: os.path.getmtime(os.path.join(OUT_DIR, f)))
                file_path = os.path.join(OUT_DIR, latest)

            df = pd.read_csv(file_path)

            # 训练模型（惰性取用 — MLTrainer 在 ml_module 内含 sklearn 全套）
            from core_config import get_ml_trainer
            _Trainer = get_ml_trainer()
            if _Trainer is None:
                return jsonify({"error": "ML 模块不可用（ml_module 未加载）"}), 500
            trainer = _Trainer(df)

            if task_type == 'regression':
                if not target_col:
                    return jsonify({"error": "请选择目标列"}), 400
                result = trainer.train_regression(
                    feature_cols=feature_cols, target_col=target_col,
                    model_name=model_name, test_size=test_size,
                    tune_hyperparams=tune_hyperparams
                )
            elif task_type == 'classification':
                if not target_col:
                    return jsonify({"error": "请选择目标列"}), 400
                result = trainer.train_classification(
                    feature_cols=feature_cols, target_col=target_col,
                    model_name=model_name, test_size=test_size,
                    tune_hyperparams=tune_hyperparams
                )
            elif task_type == 'clustering':
                result = trainer.train_clustering(
                    feature_cols=feature_cols, model_name=model_name,
                    n_clusters=n_clusters, tune_hyperparams=tune_hyperparams
                )
            else:
                return jsonify({"error": f"未知的任务类型: {task_type}"}), 400

            result['figures'] = trainer.figs

            # 保存模型 — serialise with pickle, write to OUT_DIR
            model_path = os.path.join(OUT_DIR, f"ml_model_{int(time.time() * 1000)}.pkl")
            with open(model_path, 'wb') as f:
                pickle.dump(trainer, f)
            # ---- 模型版本元数据（供版本管理 / 回滚 / 对比） ----
            meta = {
                "model_path": model_path,
                "model_file": os.path.basename(model_path),
                "version": int(time.time() * 1000),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "task_type": task_type,
                "model_name": model_name,
                "file_name": os.path.basename(file_path),
                "target_col": target_col or "",
                "feature_cols": feature_cols,
                "n_clusters": n_clusters,
                "metrics": json_safe(result.get("metrics", {})),
                "samples": result.get("samples"),
                "current": False,
            }
            try:
                with open(model_path + ".meta.json", "w", encoding="utf-8") as mf:
                    json.dump(meta, mf, ensure_ascii=False, indent=1)
            except Exception:
                pass
            try:
                lineage_mgr.record("ml_train", os.path.basename(file_path),
                                   os.path.basename(model_path),
                                   detail=f"{model_name} ({task_type})")
            except Exception:
                pass
            result['model_path'] = model_path
            result['version'] = meta["version"]

            # 训练成功后自动将新模型设为当前版本（否则 predict 默认找不到
            # ml_model_current.pkl，用户需手动回滚设版，体验断裂）
            try:
                import shutil
                current_path = os.path.join(OUT_DIR, "ml_model_current.pkl")
                shutil.copy2(model_path, current_path)
                shutil.copy2(model_path + ".meta.json", current_path + ".meta.json")
                meta["current"] = True
                # 回写 meta.json，保持磁盘与内存一致
                with open(model_path + ".meta.json", "w", encoding="utf-8") as mf:
                    json.dump(meta, mf, ensure_ascii=False, indent=1)
            except Exception as ce:
                # 不再静默：警告日志 + 响应中提示（Windows 文件锁常见）
                try:
                    from core_config import logger as _lg
                    _lg.warning(f"set-current-model failed (predict may use old model): {ce}")
                except Exception:
                    pass
                result["set_current_warning"] = f"新模型已保存但设为当前版本失败: {ce}"

            return jsonify(result)

        except Exception as e:
            traceback.print_exc()
            return exc_response(e)

    # ============================================================
    #  /api/ml/predict  (POST)
    # ============================================================
    @bp.route("/api/ml/predict", methods=["POST"])
    def ml_predict():
        """使用训练好的模型进行预测"""
        try:
            data = request.get_json() or {}
            model_path = data.get('model_path') or os.path.join(OUT_DIR, "ml_model_current.pkl")
            input_data = data.get('input_data', {})

            if not model_path or not os.path.exists(model_path):
                return jsonify({"error": "模型文件不存在（请先训练模型或选择版本）"}), 400
            # 路径约束：模型文件必须位于 OUT_DIR 内（防加载任意 pickle）
            from core_config import safe_path_or_resp
            model_path = safe_path_or_resp(model_path, "模型路径越界（仅允许 output/ 目录内）")
            if isinstance(model_path, tuple):
                return model_path
            model_path = str(model_path)

            # Secure model loading with restricted unpickler
            try:
                trainer = safe_load(model_path)
            except Exception as e:
                return jsonify({"error": f"模型加载失败: {str(e)}"}), 400

            X_new = pd.DataFrame([input_data])
            predictions = trainer.predict(X_new)

            return jsonify({
                "predictions": predictions.tolist(),
                "model_type": trainer.task_type,
            })

        except Exception as e:
            traceback.print_exc()
            return exc_response(e)

    # ============================================================
    #  /api/ml/models/versions (GET) — 模型版本列表
    # ============================================================
    @bp.route("/api/ml/models/versions")
    def ml_model_versions():
        """列出所有已保存的模型版本（含指标元数据，支持对比/回滚）"""
        try:
            versions = []
            current_path = os.path.join(OUT_DIR, "ml_model_current.pkl")
            files = sorted(Path(OUT_DIR).glob("ml_model_*.pkl"),
                           key=lambda x: x.stat().st_mtime, reverse=True)
            for p in files:
                meta_path = Path(str(p) + ".meta.json")
                meta = {}
                if meta_path.exists():
                    try:
                        meta = json.load(open(meta_path, 'r', encoding='utf-8'))
                    except Exception:
                        meta = {}
                meta.setdefault("model_file", p.name)
                meta.setdefault("model_path", str(p))
                meta.setdefault("created_at",
                                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime)))
                meta.setdefault("metrics", {})
                meta.setdefault("task_type", "")
                meta.setdefault("model_name", p.name)
                meta.setdefault("file_name", "")
                meta.setdefault("target_col", "")
                meta["size_kb"] = round(p.stat().st_size / 1024, 1)
                meta["current"] = (str(p) == current_path)
                versions.append(meta)
            return jsonify({"ok": True, "models": versions, "total": len(versions)})
        except Exception as e:
            traceback.print_exc()
            return exc_response(e)

    # ============================================================
    #  /api/ml/models/rollback (POST) — 设为当前版本（回滚）
    # ============================================================
    @bp.route("/api/ml/models/rollback", methods=["POST"])
    @limiter.limit("10 per minute")
    def ml_model_rollback():
        """将指定版本复制为 ml_model_current.pkl（当前活动模型）"""
        try:
            import shutil
            data = request.get_json() or {}
            model_path = data.get("model_path", "")
            if not model_path or not os.path.exists(model_path):
                return jsonify({"error": "模型文件不存在"}), 400
            # 先校验可安全加载，避免回滚损坏文件
            safe_load(model_path)
            current_path = os.path.join(OUT_DIR, "ml_model_current.pkl")
            shutil.copy2(model_path, current_path)
            meta_src = model_path + ".meta.json"
            if os.path.exists(meta_src):
                shutil.copy2(meta_src, current_path + ".meta.json")
            return jsonify({"ok": True, "current": current_path,
                            "message": "已设为当前模型，预测默认使用此版本"})
        except Exception as e:
            traceback.print_exc()
            return exc_response(e)

    # ============================================================
    #  /api/ml/models/compare (POST) — 两版本指标对比
    # ============================================================
    @bp.route("/api/ml/models/compare", methods=["POST"])
    @limiter.limit("10 per minute")
    def ml_model_compare():
        """对比两个模型版本的训练指标"""
        try:
            data = request.get_json() or {}
            a, b = data.get("a", ""), data.get("b", "")
            if not a or not b or not os.path.exists(a) or not os.path.exists(b):
                return jsonify({"error": "请选择两个存在的模型版本"}), 400

            def _meta(p):
                mp = p + ".meta.json"
                try:
                    return json.load(open(mp, 'r', encoding='utf-8'))
                except Exception:
                    return {}

            ma, mb = _meta(a), _meta(b)
            ka, kb = ma.get("metrics", {}), mb.get("metrics", {})
            keys = [k for k in list(ka.keys()) + list(kb.keys())
                    if k != "feature_importance"]
            keys = list(dict.fromkeys(keys))
            rows = []
            for k in keys:
                va, vb = ka.get(k, None), kb.get(k, None)
                rows.append({
                    "metric": k,
                    "a": round(va, 4) if isinstance(va, (int, float)) else (va if va is not None else "—"),
                    "b": round(vb, 4) if isinstance(vb, (int, float)) else (vb if vb is not None else "—"),
                })
            return jsonify({
                "ok": True,
                "rows": rows,
                "a": {"name": ma.get("model_name", os.path.basename(a)),
                      "file": os.path.basename(a),
                      "created_at": ma.get("created_at", "")},
                "b": {"name": mb.get("model_name", os.path.basename(b)),
                      "file": os.path.basename(b),
                      "created_at": mb.get("created_at", "")},
            })
        except Exception as e:
            traceback.print_exc()
            return exc_response(e)

    # ============================================================
    #  /api/ml/catalog  (GET)
    # ============================================================
    @bp.route("/api/ml/catalog", methods=["GET"])
    def api_ml_catalog():
        """获取算法分类目录"""
        from core_config import has_ml_enhanced, get_algorithm_catalog
        if not has_ml_enhanced():
            return jsonify({"catalog": {}})
        return jsonify({"ok": True, "catalog": get_algorithm_catalog()})

    # ============================================================
    #  /api/ml/feature-engineering  (POST)
    # ============================================================
    @bp.route("/api/ml/feature-engineering", methods=["POST"])
    def api_ml_feature_eng():
        """自动特征工程"""
        from core_config import has_ml_enhanced, get_feature_engineering
        if not has_ml_enhanced():
            return jsonify({"error": "ml_enhanced 模块未加载"}), 500
        auto_feature_engineering = get_feature_engineering()
        data = request.json
        try:
            filepath = _get_csv_file(data)
            if not filepath:
                return jsonify({"error": "请先上传数据文件"}), 400
            result = auto_feature_engineering(
                filepath, target_col=data.get("target_col"),
                task_type=data.get("task_type", "regression"),
                select_k=int(data.get("select_k", 10)),
                use_std=data.get("use_std", True), use_norm=data.get("use_norm", True),
                select_method=data.get("select_method", "f_test"))
            return jsonify(result)
        except Exception as e:
            return exc_response(e)

    # ============================================================
    #  /api/ml/evaluation-panel  (POST)
    # ============================================================
    @bp.route("/api/ml/evaluation-panel", methods=["POST"])
    def api_ml_eval_panel():
        """模型评估面板"""
        from core_config import has_ml_enhanced, get_evaluation_panel
        if not has_ml_enhanced():
            return jsonify({"error": "ml_enhanced 模块未加载"}), 500
        generate_evaluation_panel = get_evaluation_panel()
        data = request.json
        try:
            filepath = _get_csv_file(data)
            if not filepath:
                return jsonify({"error": "请先上传数据文件"}), 400
            result = generate_evaluation_panel(
                filepath, target_col=data.get("target_col", ""),
                feature_cols=data.get("feature_cols", []),
                task_type=data.get("task_type", "classification"),
                test_size=float(data.get("test_size", 0.3)),
                model_name=data.get("model_name", "logistic"))
            return jsonify({"ok": True, **result})
        except Exception as e:
            return exc_response(e)

    # ============================================================
    #  /api/feature/auto  (POST)
    # ============================================================
    @bp.route("/api/feature/auto", methods=["POST"])
    def api_feature_auto():
        """一键特征工程"""
        if not HAS_FEATURE_ENGINEERING:
            return jsonify({"error": "feature_engineering 模块未加载"}), 500
        data = request.json
        try:
            filepath = _get_csv_file(data)
            if not filepath:
                return jsonify({"error": "请先上传数据文件"}), 400
            df = pd.read_csv(filepath, encoding='utf-8-sig')
            result = auto_feature_pipeline(
                df,
                num_cols=data.get('num_cols'),
                cat_cols=data.get('cat_cols'),
                date_col=data.get('date_col'))
            output_path = OUT_DIR / f"featured_{Path(filepath).name}"
            result.to_csv(str(output_path), index=False)
            try:
                lineage_mgr.record("feature", Path(filepath).name, output_path.name,
                                   detail="一键特征工程")
            except Exception:
                pass
            return jsonify({
                "ok": True,
                "file": output_path.name,
                "orig_cols": len(df.columns),
                "new_cols": len(result.columns)})
        except Exception as e:
            return exc_response(e)

    # ============================================================
    #  /api/feature/ts  (POST)
    # ============================================================
    @bp.route("/api/feature/ts", methods=["POST"])
    def api_feature_ts():
        """时序特征"""
        if not HAS_FEATURE_ENGINEERING:
            return jsonify({"error": "feature_engineering 模块未加载"}), 500
        data = request.json
        try:
            filepath = _get_csv_file(data)
            if not filepath:
                return jsonify({"error": "请先上传数据文件"}), 400
            df = pd.read_csv(filepath, encoding='utf-8-sig')

            cols = data.get('cols', [])
            if not cols:
                cols = df.select_dtypes(include=[np.number]).columns.tolist()

            date_col = data.get('date_col')
            if date_col and date_col in df.columns:
                df = add_date_features(df, date_col)

            df = add_lag_features(df, cols, lags=data.get('lags', [1, 2, 3]))
            df = add_rolling_features(df, cols, windows=data.get('windows', [5, 10]))
            df = add_growth_features(df, cols, periods=data.get('periods', [1, 4]))
            df = add_diff_features(df, cols, periods=data.get('diff_periods', [1]))

            output_path = OUT_DIR / f"ts_features_{Path(filepath).name}"
            df.to_csv(str(output_path), index=False)
            try:
                lineage_mgr.record("feature", Path(filepath).name, output_path.name,
                                   detail="时序特征")
            except Exception:
                pass
            return jsonify({"ok": True, "file": output_path.name, "new_cols": len(df.columns)})
        except Exception as e:
            return exc_response(e)

    # ============================================================
    #  /api/feature/cross  (POST)
    # ============================================================
    @bp.route("/api/feature/cross", methods=["POST"])
    def api_feature_cross():
        """交叉特征"""
        if not HAS_FEATURE_ENGINEERING:
            return jsonify({"error": "feature_engineering 模块未加载"}), 500
        data = request.json
        try:
            filepath = _get_csv_file(data)
            if not filepath:
                return jsonify({"error": "请先上传数据文件"}), 400
            df = pd.read_csv(filepath, encoding='utf-8-sig')

            num_cols = data.get('num_cols', [])
            cat_cols = data.get('cat_cols', [])
            ops = data.get('ops', ['add', 'sub', 'mul', 'div'])

            if num_cols:
                df = add_cross_num_features(df, num_cols, ops=ops)
                df = add_cross_stat_features(df, num_cols)
            if cat_cols:
                df = add_cross_cat_features(df, cat_cols)

            output_path = OUT_DIR / f"cross_features_{Path(filepath).name}"
            df.to_csv(str(output_path), index=False)
            try:
                lineage_mgr.record("feature", Path(filepath).name, output_path.name,
                                   detail="交叉特征")
            except Exception:
                pass
            return jsonify({"ok": True, "file": output_path.name, "new_cols": len(df.columns)})
        except Exception as e:
            return exc_response(e)


    # ============================================================
    #  /api/ml/spearman (POST) — Spearman 秩相关系数分析
    # ============================================================
    @bp.route("/api/ml/spearman", methods=["POST"])
    @limiter.limit("10 per minute")
    def ml_spearman():
        """Spearman 秩相关系数（特征相关性分析）：相关系数矩阵 + 热力图 + 结论"""
        try:
            data = request.json or {}
            file = data.get("file", "")
            cols = data.get("cols") or []
            if not file or not cols:
                return jsonify({"error": "缺少文件或列"}), 400

            filepath = _get_csv_file(data)
            if not filepath:
                return jsonify({"error": "文件不存在"}), 404
            df = load_dataframe(filepath)
            if df is None:
                return jsonify({"error": "文件不存在"}), 404

            # 只保留存在的数值列
            valid = [c for c in cols if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]
            if not valid:
                return jsonify({"error": "所选列均为非数值列，无法计算 Spearman 相关系数"}), 400

            sub = df[valid].dropna()
            from scipy import stats
            n = len(valid)
            corr_mat = [[0.0] * n for _ in range(n)]
            p_mat = [[0.0] * n for _ in range(n)]
            for i in range(n):
                for j in range(n):
                    if i == j:
                        corr_mat[i][j], p_mat[i][j] = 1.0, 0.0
                    elif j > i:
                        r, p = stats.spearmanr(sub[valid[i]], sub[valid[j]])
                        corr_mat[i][j] = corr_mat[j][i] = float(r)
                        p_mat[i][j] = p_mat[j][i] = float(p)

            # 热力图（RdBu_r，-1~1）
            import base64, io
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(max(6, n * 0.8 + 2), max(5, n * 0.7 + 2)))
            im = ax.imshow(corr_mat, cmap='RdBu_r', vmin=-1, vmax=1)
            ax.set_xticks(range(n)); ax.set_xticklabels(valid, rotation=45, ha='right', fontsize=9)
            ax.set_yticks(range(n)); ax.set_yticklabels(valid, fontsize=9)
            for i in range(n):
                for j in range(n):
                    ax.text(j, i, f'{corr_mat[i][j]:.2f}', ha='center', va='center', fontsize=8,
                            color='white' if abs(corr_mat[i][j]) > 0.5 else 'black')
            fig.colorbar(im, shrink=0.8)
            plt.tight_layout()
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100)
            plt.close(fig)
            buf.seek(0)
            img_b64 = base64.b64encode(buf.read()).decode()

            # 相关系数对（按 |ρ| 降序）
            pairs = []
            for i in range(n):
                for j in range(i + 1, n):
                    pairs.append({'col1': valid[i], 'col2': valid[j],
                                  'rho': round(corr_mat[i][j], 4), 'p': round(p_mat[i][j], 6)})
            pairs.sort(key=lambda x: -abs(x['rho']))

            # 结论
            conclusion = []
            if pairs:
                top = pairs[0]
                strength = '强' if abs(top['rho']) > 0.7 else ('中' if abs(top['rho']) > 0.4 else '弱')
                direction = '正' if top['rho'] > 0 else '负'
                conclusion.append(f"最强相关：{top['col1']} ↔ {top['col2']}，ρ={top['rho']:.4f}（{strength}{direction}相关，p={top['p']:.4g}）")
                sig = [x for x in pairs if x['p'] < 0.05]
                conclusion.append(f"显著相关对（p<0.05）：{len(sig)}/{len(pairs)} 对")
                if len(pairs) > 1:
                    second = pairs[1]
                    conclusion.append(f"次强相关：{second['col1']} ↔ {second['col2']}，ρ={second['rho']:.4f}")

            return jsonify({
                'ok': True,
                'cols': valid,
                'corr_matrix': corr_mat,
                'pairs': pairs[:15],
                'conclusion': conclusion,
                'chart': 'data:image/png;base64,' + img_b64,
                'n_samples': int(len(sub)),
            })
        except Exception as e:
            return exc_response(e)
    app.register_blueprint(bp)
