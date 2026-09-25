"""
机器学习增强模块 — 特征工程 / 评估面板 / 算法分类
"""

import base64, io, json, os

import pandas as pd
import numpy as np

try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False


# ==================== 算法分类定义 ====================

ALGORITHM_CATALOG = {
    "regression": {
        "label": "回归预测",
        "desc": "连续数值预测",
        "scenarios": ["房价预测", "销量预估", "股票价格预测", "收益测算"],
        "models": [
            {"key": "linear", "name": "线性回归", "desc": "简单高效，可解释性强", "complexity": "⭐"},
            {"key": "ridge", "name": "岭回归", "desc": "L2正则，防止过拟合", "complexity": "⭐"},
            {"key": "lasso", "name": "Lasso回归", "desc": "L1正则，自动特征选择", "complexity": "⭐"},
            {"key": "elasticnet", "name": "弹性网络", "desc": "L1+L2正则组合", "complexity": "⭐"},
            {"key": "rf_regressor", "name": "随机森林回归", "desc": "集成学习，鲁棒性强", "complexity": "⭐⭐"},
            {"key": "gb_regressor", "name": "梯度提升回归", "desc": "高精度，企业级首选", "complexity": "⭐⭐⭐"},
            {"key": "knn_regressor", "name": "K近邻回归", "desc": "非参数，适合小数据", "complexity": "⭐"},
            {"key": "svr", "name": "支持向量回归", "desc": "适合高维小样本", "complexity": "⭐⭐"},
        ],
    },
    "classification": {
        "label": "分类预测",
        "desc": "离散类别判断",
        "scenarios": ["客户流失预警", "信用违约判断", "欺诈检测", "图像分类"],
        "models": [
            {"key": "logistic", "name": "逻辑回归", "desc": "线性分类，可解释性强", "complexity": "⭐"},
            {"key": "rf_classifier", "name": "随机森林分类", "desc": "集成学习，准确率高", "complexity": "⭐⭐"},
            {"key": "gb_classifier", "name": "梯度提升分类", "desc": "顶级精度，Kaggle常胜", "complexity": "⭐⭐⭐"},
            {"key": "knn_classifier", "name": "K近邻分类", "desc": "简单直观，样板足够时效果好", "complexity": "⭐"},
            {"key": "svc", "name": "支持向量机(SVM)", "desc": "适合高维数据", "complexity": "⭐⭐"},
            {"key": "decision_tree", "name": "决策树", "desc": "可视化决策路径，解释性强", "complexity": "⭐"},
        ],
    },
    "clustering": {
        "label": "聚类分析",
        "desc": "无监督分组",
        "scenarios": ["客户分群", "异常检测", "文档聚类", "图像分割"],
        "models": [
            {"key": "kmeans", "name": "K-Means", "desc": "经典聚类，速度最快", "complexity": "⭐"},
            {"key": "dbscan", "name": "DBSCAN", "desc": "密度聚类，自动发现簇数", "complexity": "⭐⭐"},
            {"key": "hierarchical", "name": "层次聚类", "desc": "生成树状聚类图", "complexity": "⭐⭐"},
            {"key": "gmm", "name": "高斯混合模型", "desc": "概率软聚类", "complexity": "⭐⭐"},
        ],
    },
}


def img_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format='png', dpi=100, bbox_inches='tight',
                facecolor='#0f111c', edgecolor='none')
    buf.seek(0)
    return base64.b64encode(buf.read()).decode()


def fig_to_html(fig):
    return fig.to_html(include_plotlyjs='cdn', full_html=False,
                       config={'displayModeBar':True,'displaylogo':False,'responsive':True})


# ==================== 自动特征工程 ====================

def auto_feature_engineering(filepath, target_col=None, task_type='regression',
                              select_k=10, use_std=True, use_norm=True,
                              select_method='f_test'):
    """
    自动特征工程：特征筛选(方差/相关性/MutualInfo/Spearman)、标准化、归一化

    Args:
        select_method: 特征筛选方法
            - 'f_test'     : F 检验（f_classif / f_regression）
            - 'spearman'   : 斯皮尔曼秩相关（按 |特征-目标秩相关系数| 排序取 top-k）
            - 'mutual_info': 互信息
    """
    from sklearn.preprocessing import StandardScaler, MinMaxScaler
    from sklearn.feature_selection import SelectKBest, f_regression, f_classif, mutual_info_regression, VarianceThreshold

    df = pd.read_csv(filepath)
    numeric_df = df.select_dtypes(include=[np.number])
    
    if numeric_df.shape[1] < 2:
        return {"error": "数据中数值列不足（<2列）", "status": "failed"}
    
    # 1. 方差筛选（移除常数列）
    selector = VarianceThreshold(threshold=0.01)
    feature_cols = [c for c in numeric_df.columns if c != target_col or target_col is None]
    
    if target_col and target_col in numeric_df.columns:
        y = numeric_df[target_col].fillna(numeric_df[target_col].median())
        X = numeric_df[feature_cols].fillna(numeric_df[feature_cols].median())
    else:
        y = None
        X = numeric_df[feature_cols].fillna(numeric_df[feature_cols].median())
    
    # 方差过滤
    var_mask = X.var() > 0.01
    low_var_cols = [c for c in feature_cols if c not in X.columns[var_mask]]
    X_var = X.loc[:, var_mask]
    
    # 2. 标准化
    std_cols = X_var.columns.tolist()
    if use_std:
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X_var)
    else:
        X_scaled = X_var.values
    
    # 3. 归一化（在标准化之后）
    if use_norm:
        normalizer = MinMaxScaler()
        X_scaled = normalizer.fit_transform(
            pd.DataFrame(X_scaled, columns=std_cols))
    
    # 4. 特征筛选
    k = min(select_k, X_scaled.shape[1])
    selected_features = []
    feature_scores = {}
    
    if y is not None:
        try:
            if select_method == 'spearman':
                # 斯皮尔曼秩相关打分：|秩相关系数| 越大越重要
                X_df = pd.DataFrame(X_scaled, columns=std_cols)
                y_series = pd.Series(y)
                scores = []
                for col in std_cols:
                    try:
                        s = X_df[col].corr(y_series, method='spearman')
                        scores.append(abs(s) if not pd.isna(s) else 0.0)
                    except Exception:
                        scores.append(0.0)
                scores = np.array(scores)
                order = np.argsort(-scores)[:k]
                selected_features = [std_cols[i] for i in order]
                feature_scores = {std_cols[i]: round(float(scores[i]), 4) for i in order}
            elif select_method == 'mutual_info':
                # 互信息打分
                from sklearn.feature_selection import mutual_info_classif
                if task_type == 'classification':
                    mi = mutual_info_classif(X_scaled, y)
                else:
                    mi = mutual_info_regression(X_scaled, y)
                order = np.argsort(-mi)[:k]
                selected_features = [std_cols[i] for i in order]
                feature_scores = {std_cols[i]: round(float(mi[i]), 4) for i in order}
            else:
                # F 检验（默认）
                scorer = f_classif if task_type == 'classification' else f_regression
                selector_k = SelectKBest(scorer, k=k)
                selector_k.fit(X_scaled, y)
                scores = selector_k.scores_
                mask = selector_k.get_support()
                selected_features = [std_cols[i] for i in range(len(std_cols)) if mask[i]]
                feature_scores = {std_cols[i]: round(float(scores[i]), 4)
                                  for i in range(len(std_cols)) if mask[i]}
        except Exception:
            selected_features = std_cols[:k]
            feature_scores = {c: 1.0 for c in selected_features}
    else:
        selected_features = std_cols[:k]
        feature_scores = {c: float(X_var[c].var()) for c in selected_features}
    
    # 按分数排序
    feature_scores = dict(sorted(feature_scores.items(), key=lambda x: x[1], reverse=True))
    
    # 可视化
    chart = None
    if HAS_MPL and len(selected_features) > 0:
        fig, axes = plt.subplots(1, 3, figsize=(12, 4))
        fig.patch.set_facecolor('#0f111c')
        
        # 特征方差
        vars_vals = X_var[selected_features[:8]].var()
        axes[0].barh(range(len(vars_vals)), vars_vals.values,
                     color='#4caf50', alpha=0.7)
        axes[0].set_yticks(range(len(vars_vals)))
        axes[0].set_yticklabels([c[:12] for c in vars_vals.index], fontsize=8)
        axes[0].set_title('特征方差', color='#e0e0e0', fontsize=10)
        
        # 特征重要性得分
        scores_items = list(feature_scores.items())[:8]
        axes[1].barh(range(len(scores_items)), [s[1] for s in scores_items],
                     color='#7c4dff', alpha=0.7)
        axes[1].set_yticks(range(len(scores_items)))
        axes[1].set_yticklabels([s[0][:12] for s in scores_items], fontsize=8)
        axes[1].set_title('特征重要性得分', color='#e0e0e0', fontsize=10)
        
        # 标准化前后对比
        raw_sample = X_var[selected_features[0]].values[:100]
        axes[2].hist(raw_sample - raw_sample.mean(), bins=20, alpha=0.5, color='#2196f3', label='原始')
        if use_std:
            axes[2].hist((X_scaled[:, 0] - X_scaled[:, 0].mean())[:100], bins=20,
                         alpha=0.5, color='#ff9800', label='标准化')
        axes[2].legend(fontsize=8)
        axes[2].set_title('标准化效果', color='#e0e0e0', fontsize=10)
        
        for ax in axes:
            ax.set_facecolor('#151725')
            ax.tick_params(colors='#a0a0b0', labelsize=8)
        
        plt.tight_layout()
        chart = img_to_b64(fig)
        plt.close()
    
    return {
        "status": "completed",
        "total_features": len(feature_cols),
        "low_variance_removed": low_var_cols,
        "selected_features": selected_features,
        "feature_scores": feature_scores,
        "standardization_applied": use_std,
        "normalization_applied": use_norm,
        "chart": chart,
    }


# ==================== 模型评估面板 ====================

def generate_evaluation_panel(filepath, target_col, feature_cols, task_type='classification',
                               test_size=0.3, model_name='logistic'):
    """
    生成评估面板：混淆矩阵、ROC曲线、特征重要性
    """
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import confusion_matrix, roc_curve, auc

    df = pd.read_csv(filepath)
    numeric = df.select_dtypes(include=[np.number])
    
    if target_col not in df.columns:
        return {"error": f"目标列 '{target_col}' 不存在"}
    
    # 处理分类任务的标签问题
    if task_type == 'classification':
        from sklearn.preprocessing import LabelEncoder
        le = LabelEncoder()
        if target_col in numeric.columns:
            # 数值型分类目标可能需要离散化
            y_raw = df[target_col].fillna(df[target_col].median())
            unique_vals = y_raw.nunique()
            if unique_vals > 10:
                # 二值化：中位数以上=1
                median = y_raw.median()
                y = (y_raw > median).astype(int)
                is_binarized = True
            else:
                y = y_raw.astype(int)
                is_binarized = False
        else:
            y = le.fit_transform(df[target_col].fillna(df[target_col].mode()[0]))
            is_binarized = False
    else:
        y = df[target_col].fillna(df[target_col].median())
    
    # 特征矩阵
    valid_features = [c for c in feature_cols if c in numeric.columns and c != target_col]
    if not valid_features:
        return {"error": "无有效特征列"}
    
    X = numeric[valid_features].fillna(numeric[valid_features].median())
    
    # 分割
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=42, stratify=y if task_type=='classification' else None)
    
    # 训练模型
    model = _get_model(model_name, task_type)
    model.fit(X_train, y_train)
    y_pred = model.predict(X_test)
    
    results = {
        "model": model_name,
        "task_type": task_type,
        "n_features": len(valid_features),
        "n_train": len(X_train),
        "n_test": len(X_test),
    }
    
    charts = {}
    
    # 混淆矩阵 + ROC (分类)
    if task_type == 'classification':
        # 混淆矩阵
        cm = confusion_matrix(y_test, y_pred)
        n_classes = cm.shape[0]
        
        if HAS_PLOTLY:
            fig = go.Figure(data=go.Heatmap(
                z=cm, text=cm, texttemplate='%{text}',
                colorscale='Blues', x=list(range(n_classes)),
                y=list(range(n_classes)),
                textfont=dict(size=14, color='white' if n_classes <= 3 else 'black'),
                hovertemplate='真实: %{y}<br>预测: %{x}<br>数量: %{z}<extra></extra>',
            ))
            fig.update_layout(
                title='混淆矩阵', paper_bgcolor='#0f111c',
                plot_bgcolor='#151725', font=dict(color='#e0e0e0'),
                xaxis=dict(title='预测'), yaxis=dict(title='真实'),
                height=350, margin=dict(l=50, r=20, t=50, b=50),
            )
            charts['confusion_matrix'] = fig_to_html(fig)
        
        # ROC 曲线 (二分类)
        if n_classes <= 2:
            try:
                if hasattr(model, 'predict_proba'):
                    y_score = model.predict_proba(X_test)[:, 1]
                elif hasattr(model, 'decision_function'):
                    y_score = model.decision_function(X_test)
                else:
                    y_score = y_pred
                
                if len(np.unique(y_test)) > 1:
                    fpr, tpr, _ = roc_curve(y_test, y_score)
                    roc_auc = auc(fpr, tpr)
                    
                    fig2 = go.Figure()
                    fig2.add_trace(go.Scatter(x=fpr, y=tpr, name=f'AUC={roc_auc:.3f}',
                                              line=dict(color='#7c4dff', width=2),
                                              fill='tozeroy', fillcolor='rgba(124,77,255,0.1)'))
                    fig2.add_trace(go.Scatter(x=[0,1], y=[0,1], name='随机',
                                              line=dict(color='#666', dash='dash')))
                    fig2.update_layout(
                        title=f'ROC 曲线 (AUC={roc_auc:.3f})',
                        paper_bgcolor='#0f111c', plot_bgcolor='#151725',
                        font=dict(color='#e0e0e0'), height=350,
                        xaxis_title='假阳性率', yaxis_title='真阳性率',
                        margin=dict(l=50, r=20, t=50, b=50),
                    )
                    charts['roc_curve'] = fig_to_html(fig2)
                    results['auc'] = round(float(roc_auc), 4)
            except Exception:
                pass
    
    # 回归任务：预测值 vs 实际值散点图
    if task_type == 'regression':
        if HAS_MPL:
            fig4, ax4 = plt.subplots(figsize=(8, 6))
            fig4.patch.set_facecolor('#ffffff')
            ax4.set_facecolor('#f8f9fc')
            ax4.grid(True, alpha=0.3, color='#e0e0e0', linestyle='--')
            
            ax4.scatter(y_test, y_pred, alpha=0.6, s=40, c='#FF6B6B', edgecolors='white', linewidth=0.5)
            
            # 对角线（完美预测线）
            min_val = min(y_test.min(), y_pred.min())
            max_val = max(y_test.max(), y_pred.max())
            ax4.plot([min_val, max_val], [min_val, max_val], 'r--', lw=2, alpha=0.7, label='完美预测线')
            
            ax4.set_xlabel('实际值', fontsize=11, color='#333')
            ax4.set_ylabel('预测值', fontsize=11, color='#333')
            ax4.set_title('预测值 vs 实际值', fontsize=13, color='#333', fontweight='600')
            ax4.legend(fontsize=10)
            ax4.tick_params(colors='#333')
            ax4.spines['bottom'].set_color('#cccccc')
            ax4.spines['left'].set_color('#cccccc')
            ax4.spines['top'].set_visible(False)
            ax4.spines['right'].set_visible(False)
            
            plt.tight_layout()
            charts['predicted_vs_actual'] = img_to_b64(fig4)
            plt.close()
    
    # 特征重要性 (全局)
    importance = _get_feature_importance(model, valid_features)
    if importance:
        if HAS_MPL:
            fig3, ax = plt.subplots(figsize=(8, max(4, len(importance)//4)))
            fig3.patch.set_facecolor('#ffffff')
            ax.set_facecolor('#f8f9fc')
            
            items = dict(list(importance.items())[:12])
            colors_plt = ['#FF6B6B' if v >= 0 else '#ef5350' for v in items.values()]
            ax.barh(range(len(items)), list(items.values()),
                    color=colors_plt, alpha=0.8, height=0.6)
            ax.set_yticks(range(len(items)))
            ax.set_yticklabels(list(items.keys()), fontsize=9, color='#333')
            ax.set_xlabel('重要性', fontsize=10, color='#333')
            ax.set_title('特征重要性', color='#333', fontsize=12, fontweight='600')
            ax.tick_params(colors='#333')
            ax.invert_yaxis()
            ax.spines['bottom'].set_color('#cccccc')
            ax.spines['left'].set_color('#cccccc')
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            
            plt.tight_layout()
            charts['feature_importance'] = img_to_b64(fig3)
            plt.close()
        
        results['top_features'] = list(importance.items())[:5]
    
    results['charts'] = charts
    return results


def _get_model(model_name, task_type):
    from sklearn.linear_model import (
        LinearRegression, Ridge, Lasso, ElasticNet, LogisticRegression
    )
    from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
    from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
    from sklearn.neighbors import KNeighborsRegressor, KNeighborsClassifier
    from sklearn.svm import SVR, SVC
    from sklearn.tree import DecisionTreeClassifier
    from sklearn.cluster import KMeans, DBSCAN
    
    models = {
        'linear': LinearRegression(),
        'ridge': Ridge(alpha=1),
        'lasso': Lasso(alpha=0.01, max_iter=3000),
        'elasticnet': ElasticNet(alpha=0.01, max_iter=3000),
        'rf_regressor': RandomForestRegressor(n_estimators=100, random_state=42),
        'gb_regressor': GradientBoostingRegressor(n_estimators=100, random_state=42),
        'knn_regressor': KNeighborsRegressor(n_neighbors=5),
        'svr': SVR(kernel='rbf'),
        'logistic': LogisticRegression(max_iter=2000, random_state=42),
        'rf_classifier': RandomForestClassifier(n_estimators=100, random_state=42),
        'gb_classifier': GradientBoostingClassifier(n_estimators=100, random_state=42),
        'knn_classifier': KNeighborsClassifier(n_neighbors=5),
        'svc': SVC(kernel='rbf', probability=True, random_state=42),
        'decision_tree': DecisionTreeClassifier(max_depth=5, random_state=42),
        'kmeans': KMeans(n_clusters=3, random_state=42, n_init=10),
        'dbscan': DBSCAN(eps=0.5, min_samples=5),
    }
    return models.get(model_name, LogisticRegression(max_iter=2000))


def _get_feature_importance(model, feature_names):
    """提取特征重要性"""
    importance = {}
    try:
        if hasattr(model, 'feature_importances_'):
            imp = model.feature_importances_
            importance = {feature_names[i]: round(float(imp[i]), 4)
                          for i in range(min(len(feature_names), len(imp)))}
        elif hasattr(model, 'coef_'):
            coef = model.coef_
            if coef.ndim > 1:
                coef = coef[0]
            importance = {feature_names[i]: round(float(coef[i]), 4)
                          for i in range(min(len(feature_names), len(coef)))}
        
        # 按绝对值排序
        importance = dict(sorted(importance.items(), key=lambda x: abs(x[1]), reverse=True))
    except Exception:
        pass
    return importance
