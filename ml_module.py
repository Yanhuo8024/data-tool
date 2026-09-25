#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
机器学习模块 - 支持监督学习和无监督学习算法
"""
import json
import traceback
import numpy as np
import pandas as pd
from typing import Dict, Any, List, Tuple, Optional

# 监督学习
from sklearn.linear_model import LinearRegression, Ridge, Lasso, LogisticRegression
from sklearn.tree import DecisionTreeRegressor, DecisionTreeClassifier
from sklearn.ensemble import RandomForestRegressor, RandomForestClassifier, GradientBoostingRegressor
from sklearn.svm import SVR, SVC
from sklearn.neighbors import KNeighborsRegressor, KNeighborsClassifier
from sklearn.naive_bayes import GaussianNB

# 无监督学习
from sklearn.cluster import KMeans, DBSCAN, AgglomerativeClustering
from sklearn.decomposition import PCA
from sklearn.mixture import GaussianMixture

# 模型评估
from sklearn.model_selection import train_test_split, cross_val_score, GridSearchCV
from sklearn.metrics import (
    mean_squared_error, mean_absolute_error, r2_score,
    accuracy_score, precision_score, recall_score, f1_score,
    confusion_matrix, classification_report,
    silhouette_score, calinski_harabasz_score
)
from sklearn.preprocessing import StandardScaler, LabelEncoder, OneHotEncoder
from sklearn.impute import SimpleImputer

# 高级模型
try:
    from xgboost import XGBRegressor, XGBClassifier
    HAS_XGBOOST = True
except ImportError:
    HAS_XGBOOST = False
    XGBRegressor = None
    XGBClassifier = None

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns
from io import BytesIO
import base64


class MLTrainer:
    """机器学习训练器"""
    
    # 支持的算法
    REGRESSION_MODELS = {
        'linear_regression': {'label': '线性回归', 'class': LinearRegression, 'params': {}},
        'ridge': {'label': '岭回归', 'class': Ridge, 'params': {'alpha': [0.1, 1.0, 10.0]}},
        'lasso': {'label': 'Lasso回归', 'class': Lasso, 'params': {'alpha': [0.1, 1.0, 10.0]}},
        'decision_tree_reg': {'label': '决策树回归', 'class': DecisionTreeRegressor, 
                              'params': {'max_depth': [3, 5, 10], 'min_samples_split': [2, 5]}},
        'random_forest_reg': {'label': '随机森林回归', 'class': RandomForestRegressor,
                              'params': {'n_estimators': [50, 100], 'max_depth': [5, 10]}},
        'gradient_boosting_reg': {'label': '梯度提升回归', 'class': GradientBoostingRegressor,
                                  'params': {'n_estimators': [50, 100], 'learning_rate': [0.1, 0.01]}},
        'svr': {'label': '支持向量回归', 'class': SVR, 'params': {'C': [1.0, 10.0], 'kernel': ['rbf', 'linear']}},
        'knn_reg': {'label': 'K近邻回归', 'class': KNeighborsRegressor, 'params': {'n_neighbors': [3, 5, 10]}},
    }
    
    CLASSIFICATION_MODELS = {
        'logistic_regression': {'label': '逻辑回归', 'class': LogisticRegression, 'params': {'C': [0.1, 1.0, 10.0]}},
        'decision_tree_clf': {'label': '决策树分类', 'class': DecisionTreeClassifier,
                              'params': {'max_depth': [3, 5, 10], 'min_samples_split': [2, 5]}},
        'random_forest_clf': {'label': '随机森林分类', 'class': RandomForestClassifier,
                              'params': {'n_estimators': [50, 100], 'max_depth': [5, 10]}},
        'svm': {'label': '支持向量机', 'class': SVC, 'params': {'C': [1.0, 10.0], 'kernel': ['rbf', 'linear']}},
        'knn_clf': {'label': 'K近邻分类', 'class': KNeighborsClassifier, 'params': {'n_neighbors': [3, 5, 10]}},
        'naive_bayes': {'label': '朴素贝叶斯', 'class': GaussianNB, 'params': {}},
    }
    
    CLUSTERING_MODELS = {
        'kmeans': {'label': 'K-Means聚类', 'class': KMeans, 'params': {'n_clusters': [2, 3, 4, 5, 6, 8]}},
        'dbscan': {'label': 'DBSCAN聚类', 'class': DBSCAN, 'params': {'eps': [0.3, 0.5, 0.7], 'min_samples': [5, 10]}},
        'agglomerative': {'label': '层次聚类', 'class': AgglomerativeClustering, 'params': {'n_clusters': [2, 3, 4, 5]}},
        'gmm': {'label': '高斯混合模型', 'class': GaussianMixture, 'params': {'n_components': [2, 3, 4, 5]}},
    }

    def __init__(self, df: pd.DataFrame):
        self.df = df.copy()
        self.scaler = StandardScaler()
        self.label_encoders = {}
        self.feature_cols = []
        self.target_col = None
        self.model = None
        self.model_type = None
        self.task_type = None  # 'regression', 'classification', 'clustering'
        self.metrics = {}
        self.figs = {}
        
    def _preprocess_features(self, X: pd.DataFrame, fit: bool = True) -> np.ndarray:
        """预处理特征：编码分类变量，填充缺失值，标准化"""
        X_processed = X.copy()
        
        # 分离数值和分类列
        numeric_cols = X_processed.select_dtypes(include=[np.number]).columns.tolist()
        categorical_cols = X_processed.select_dtypes(include=['str', 'object', 'category']).columns.tolist()
        
        # 填充数值缺失值
        if numeric_cols:
            imputer = SimpleImputer(strategy='median')
            if fit:
                X_processed[numeric_cols] = imputer.fit_transform(X_processed[numeric_cols])
                self.numeric_imputer = imputer
            else:
                X_processed[numeric_cols] = self.numeric_imputer.transform(X_processed[numeric_cols])
        
        # 编码分类变量
        for col in categorical_cols:
            X_processed[col] = X_processed[col].fillna('missing')
            if fit:
                le = LabelEncoder()
                X_processed[col] = le.fit_transform(X_processed[col].astype(str))
                self.label_encoders[col] = le
            else:
                le = self.label_encoders.get(col)
                if le:
                    # 处理未知类别
                    X_processed[col] = X_processed[col].apply(
                        lambda x: le.transform([x])[0] if x in le.classes_ else -1
                    )
        
        # 标准化
        if fit:
            return self.scaler.fit_transform(X_processed)
        return self.scaler.transform(X_processed)
    
    def train_regression(self, feature_cols: List[str], target_col: str, 
                         model_name: str = 'linear_regression', 
                         test_size: float = 0.2, 
                         tune_hyperparams: bool = False) -> Dict[str, Any]:
        """训练回归模型"""
        self.task_type = 'regression'
        self.feature_cols = feature_cols
        self.target_col = target_col
        
        # 准备数据
        X = self.df[feature_cols].copy()
        y = self.df[target_col].copy()
        
        # 删除目标列中的缺失值
        valid_idx = y.notna()
        X = X[valid_idx]
        y = y[valid_idx]
        
        # 预处理
        X_processed = self._preprocess_features(X, fit=True)
        
        # 检查最小样本量
        if len(y) < 10:
            raise ValueError(f"数据量太少（仅{len(y)}条），至少需要10条数据才能训练模型")
        
        # 划分训练集和测试集
        X_train, X_test, y_train, y_test = train_test_split(
            X_processed, y.values, test_size=test_size, random_state=42
        )
        
        # 获取模型
        model_info = self.REGRESSION_MODELS.get(model_name, self.REGRESSION_MODELS['linear_regression'])
        model_class = model_info['class']
        
        # 动态调整交叉验证折数
        n_samples = len(y)
        cv_folds = min(5, max(2, n_samples // 2))
        
        # 训练模型
        if tune_hyperparams and model_info['params']:
            actual_cv = min(3, cv_folds)
            grid_search = GridSearchCV(model_class(), model_info['params'], cv=actual_cv, scoring='neg_mean_squared_error')
            grid_search.fit(X_train, y_train)
            self.model = grid_search.best_estimator_
            best_params = grid_search.best_params_
        else:
            self.model = model_class()
            self.model.fit(X_train, y_train)
            best_params = {}
        
        # 预测
        y_pred_train = self.model.predict(X_train)
        y_pred_test = self.model.predict(X_test)
        
        # 计算指标
        self.metrics = {
            'train_mse': float(mean_squared_error(y_train, y_pred_train)),
            'train_rmse': float(np.sqrt(mean_squared_error(y_train, y_pred_train))),
            'train_mae': float(mean_absolute_error(y_train, y_pred_train)),
            'train_r2': float(r2_score(y_train, y_pred_train)),
            'test_mse': float(mean_squared_error(y_test, y_pred_test)),
            'test_rmse': float(np.sqrt(mean_squared_error(y_test, y_pred_test))),
            'test_mae': float(mean_absolute_error(y_test, y_pred_test)),
            'test_r2': float(r2_score(y_test, y_pred_test)),
            'best_params': best_params,
        }
        
        # 交叉验证
        cv_scores = cross_val_score(self.model, X_processed, y.values, cv=cv_folds, scoring='r2')
        self.metrics['cv_r2_mean'] = float(cv_scores.mean())
        self.metrics['cv_r2_std'] = float(cv_scores.std())
        
        # 特征重要性
        if hasattr(self.model, 'feature_importances_'):
            self.metrics['feature_importance'] = dict(zip(feature_cols, 
                                                           self.model.feature_importances_.tolist()))
        elif hasattr(self.model, 'coef_'):
            self.metrics['feature_importance'] = dict(zip(feature_cols, 
                                                           np.abs(self.model.coef_).tolist()))
        
        # 生成可视化
        self._plot_regression_results(X_test, y_test, y_pred_test)
        
        return {
            'model_name': model_info['label'],
            'model_type': 'regression',
            'metrics': self.metrics,
            'feature_cols': feature_cols,
            'target_col': target_col,
            'samples': len(y),
            'test_size': len(y_test),
        }
    
    def train_classification(self, feature_cols: List[str], target_col: str,
                             model_name: str = 'logistic_regression',
                             test_size: float = 0.2,
                             tune_hyperparams: bool = False) -> Dict[str, Any]:
        """训练分类模型"""
        self.task_type = 'classification'
        self.feature_cols = feature_cols
        self.target_col = target_col
        
        # 准备数据
        X = self.df[feature_cols].copy()
        y = self.df[target_col].copy()
        
        # 删除目标列中的缺失值
        valid_idx = y.notna()
        X = X[valid_idx]
        y = y[valid_idx]
        
        # 编码目标变量（如果是字符串）
        if y.dtype == 'object':
            le = LabelEncoder()
            y = le.fit_transform(y.astype(str))
            self.target_encoder = le
        
        # 预处理
        X_processed = self._preprocess_features(X, fit=True)
        
        # 检查最小样本量
        if len(y) < 10:
            raise ValueError(f"数据量太少（仅{len(y)}条），至少需要10条数据才能训练模型")
        
        # 检查每个类别的样本数
        class_counts = np.bincount(y.astype(int))
        min_class_count = class_counts.min()
        
        # 动态调整交叉验证折数
        n_samples = len(y)
        cv_folds = min(5, max(2, min_class_count, n_samples // 2))
        
        # 划分训练集和测试集（如果数据量太小，不进行分层采样）
        if min_class_count >= 2:
            X_train, X_test, y_train, y_test = train_test_split(
                X_processed, y, test_size=test_size, random_state=42, stratify=y
            )
        else:
            X_train, X_test, y_train, y_test = train_test_split(
                X_processed, y, test_size=test_size, random_state=42
            )
        
        # 获取模型
        model_info = self.CLASSIFICATION_MODELS.get(model_name, self.CLASSIFICATION_MODELS['logistic_regression'])
        model_class = model_info['class']
        
        # 训练模型
        if tune_hyperparams and model_info['params']:
            actual_cv = min(3, cv_folds)
            grid_search = GridSearchCV(model_class(), model_info['params'], cv=actual_cv, scoring='accuracy')
            grid_search.fit(X_train, y_train)
            self.model = grid_search.best_estimator_
            best_params = grid_search.best_params_
        else:
            self.model = model_class()
            self.model.fit(X_train, y_train)
            best_params = {}
        
        # 预测
        y_pred_train = self.model.predict(X_train)
        y_pred_test = self.model.predict(X_test)
        
        # 计算指标
        self.metrics = {
            'train_accuracy': float(accuracy_score(y_train, y_pred_train)),
            'test_accuracy': float(accuracy_score(y_test, y_pred_test)),
            'test_precision': float(precision_score(y_test, y_pred_test, average='weighted', zero_division=0)),
            'test_recall': float(recall_score(y_test, y_pred_test, average='weighted', zero_division=0)),
            'test_f1': float(f1_score(y_test, y_pred_test, average='weighted', zero_division=0)),
            'best_params': best_params,
        }
        
        # 交叉验证
        cv_scores = cross_val_score(self.model, X_processed, y, cv=cv_folds, scoring='accuracy')
        self.metrics['cv_accuracy_mean'] = float(cv_scores.mean())
        self.metrics['cv_accuracy_std'] = float(cv_scores.std())
        
        # 混淆矩阵
        cm = confusion_matrix(y_test, y_pred_test)
        self.metrics['confusion_matrix'] = cm.tolist()
        
        # 特征重要性
        if hasattr(self.model, 'feature_importances_'):
            self.metrics['feature_importance'] = dict(zip(feature_cols, 
                                                           self.model.feature_importances_.tolist()))
        elif hasattr(self.model, 'coef_'):
            coef_abs = np.abs(self.model.coef_)
            if coef_abs.ndim > 1:
                coef_abs = coef_abs.mean(axis=0)
            self.metrics['feature_importance'] = dict(zip(feature_cols, coef_abs.tolist()))
        
        # 生成可视化
        self._plot_classification_results(y_test, y_pred_test, cm)
        
        return {
            'model_name': model_info['label'],
            'model_type': 'classification',
            'metrics': self.metrics,
            'feature_cols': feature_cols,
            'target_col': target_col,
            'samples': len(y),
            'classes': len(np.unique(y)),
        }
    
    def train_clustering(self, feature_cols: List[str], 
                         model_name: str = 'kmeans',
                         n_clusters: int = 3,
                         tune_hyperparams: bool = False) -> Dict[str, Any]:
        """训练聚类模型"""
        self.task_type = 'clustering'
        self.feature_cols = feature_cols
        
        # 准备数据
        X = self.df[feature_cols].copy()
        X_processed = self._preprocess_features(X, fit=True)
        
        # 获取模型
        model_info = self.CLUSTERING_MODELS.get(model_name, self.CLUSTERING_MODELS['kmeans'])
        model_class = model_info['class']
        
        # 训练模型
        if model_name == 'kmeans':
            self.model = model_class(n_clusters=n_clusters, random_state=42, n_init=10)
        elif model_name == 'dbscan':
            self.model = model_class(eps=0.5, min_samples=5)
        elif model_name == 'gmm':
            self.model = model_class(n_components=n_clusters, random_state=42)
        else:
            self.model = model_class(n_clusters=n_clusters)
        
        labels = self.model.fit_predict(X_processed)
        
        # 计算指标
        n_clusters_found = len(set(labels)) - (1 if -1 in labels else 0)
        
        self.metrics = {
            'n_clusters_found': n_clusters_found,
            'n_noise_points': int((labels == -1).sum()) if -1 in labels else 0,
        }
        
        # 轮廓系数（仅当聚类数 > 1 时有效）
        if n_clusters_found > 1 and len(set(labels)) > 1:
            mask = labels != -1
            if mask.sum() > n_clusters_found:
                self.metrics['silhouette_score'] = float(silhouette_score(X_processed[mask], labels[mask]))
                self.metrics['calinski_harabasz_score'] = float(calinski_harabasz_score(X_processed[mask], labels[mask]))
        
        # 每个簇的样本数
        unique, counts = np.unique(labels, return_counts=True)
        self.metrics['cluster_distribution'] = {str(int(u)): int(c) for u, c in zip(unique, counts)}
        
        # 簇中心（如果支持）
        if hasattr(self.model, 'cluster_centers_'):
            centers = self.model.cluster_centers_
            self.metrics['cluster_centers'] = pd.DataFrame(
                centers, 
                columns=feature_cols,
                index=[f'Cluster_{i}' for i in range(len(centers))]
            ).to_dict()
        
        # 保存聚类标签
        self.df['cluster_label'] = labels
        
        # 生成可视化
        self._plot_clustering_results(X_processed, labels, feature_cols)
        
        return {
            'model_name': model_info['label'],
            'model_type': 'clustering',
            'metrics': self.metrics,
            'feature_cols': feature_cols,
            'samples': len(X),
            'n_clusters': n_clusters_found,
        }
    
    def _plot_regression_results(self, X_test, y_test, y_pred_test):
        """绘制回归结果图表（明亮主题）"""
        # 1. 预测值 vs 真实值
        fig, ax = plt.subplots(figsize=(8, 6))
        fig.patch.set_facecolor('#ffffff')
        ax.set_facecolor('#f8f9fc')
        ax.grid(True, alpha=0.3, color='#e0e0e0', linestyle='--')
        ax.scatter(y_test, y_pred_test, alpha=0.6, edgecolors='white', linewidth=0.5, c='#FF6B6B', s=40)
        min_val = min(y_test.min(), y_pred_test.min())
        max_val = max(y_test.max(), y_pred_test.max())
        ax.plot([min_val, max_val], [min_val, max_val], 'r--', lw=2, label='完美预测线', alpha=0.7)
        ax.set_xlabel('真实值', fontsize=12, color='#333')
        ax.set_ylabel('预测值', fontsize=12, color='#333')
        ax.set_title('预测值 vs 真实值', fontsize=14, fontweight='bold', color='#333')
        ax.legend(fontsize=10)
        ax.tick_params(colors='#333')
        ax.spines['bottom'].set_color('#cccccc')
        ax.spines['left'].set_color('#cccccc')
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        plt.tight_layout()
        self.figs['prediction_vs_actual'] = self._fig_to_base64(fig)
        plt.close(fig)
        
        # 2. 残差图
        fig, ax = plt.subplots(figsize=(8, 6))
        fig.patch.set_facecolor('#ffffff')
        ax.set_facecolor('#f8f9fc')
        ax.grid(True, alpha=0.3, color='#e0e0e0', linestyle='--')
        residuals = y_test - y_pred_test
        ax.scatter(y_pred_test, residuals, alpha=0.6, edgecolors='white', linewidth=0.5, c='#8b5cf6', s=40)
        ax.axhline(y=0, color='r', linestyle='--', lw=2, alpha=0.7)
        ax.set_xlabel('预测值', fontsize=12, color='#333')
        ax.set_ylabel('残差', fontsize=12, color='#333')
        ax.set_title('残差分析图', fontsize=14, fontweight='bold', color='#333')
        ax.tick_params(colors='#333')
        ax.spines['bottom'].set_color('#cccccc')
        ax.spines['left'].set_color('#cccccc')
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        plt.tight_layout()
        self.figs['residuals'] = self._fig_to_base64(fig)
        plt.close(fig)
        
        # 3. 特征重要性
        if 'feature_importance' in self.metrics:
            fig, ax = plt.subplots(figsize=(10, 6))
            fig.patch.set_facecolor('#ffffff')
            ax.set_facecolor('#f8f9fc')
            importance = self.metrics['feature_importance']
            sorted_items = sorted(importance.items(), key=lambda x: x[1], reverse=True)
            names, values = zip(*sorted_items)
            colors = ['#FF6B6B' if v >= 0 else '#ef5350' for v in values]
            bars = ax.barh(range(len(names)), values, color=colors)
            ax.set_yticks(range(len(names)))
            ax.set_yticklabels(names, color='#333')
            ax.set_xlabel('重要性', fontsize=12, color='#333')
            ax.set_title('特征重要性', fontsize=14, fontweight='bold', color='#333')
            ax.tick_params(colors='#333')
            ax.invert_yaxis()
            ax.spines['bottom'].set_color('#cccccc')
            ax.spines['left'].set_color('#cccccc')
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            plt.tight_layout()
            self.figs['feature_importance'] = self._fig_to_base64(fig)
            plt.close(fig)
    
    def _plot_classification_results(self, y_test, y_pred, cm):
        """绘制分类结果图表"""
        # 1. 混淆矩阵热力图
        fig, ax = plt.subplots(figsize=(8, 6))
        sns.heatmap(cm, annot=True, fmt='d', cmap='Blues', ax=ax, cbar_kws={'label': '样本数'})
        ax.set_xlabel('预测标签', fontsize=12)
        ax.set_ylabel('真实标签', fontsize=12)
        ax.set_title('混淆矩阵', fontsize=14, fontweight='bold')
        self.figs['confusion_matrix'] = self._fig_to_base64(fig)
        plt.close(fig)
        
        # 2. 特征重要性
        if 'feature_importance' in self.metrics:
            fig, ax = plt.subplots(figsize=(10, 6))
            importance = self.metrics['feature_importance']
            sorted_items = sorted(importance.items(), key=lambda x: x[1], reverse=True)
            names, values = zip(*sorted_items)
            colors = plt.cm.viridis(np.linspace(0.3, 0.9, len(names)))
            bars = ax.barh(range(len(names)), values, color=colors)
            ax.set_yticks(range(len(names)))
            ax.set_yticklabels(names)
            ax.set_xlabel('重要性', fontsize=12)
            ax.set_title('特征重要性', fontsize=14, fontweight='bold')
            ax.invert_yaxis()
            plt.tight_layout()
            self.figs['feature_importance'] = self._fig_to_base64(fig)
            plt.close(fig)
    
    def _plot_clustering_results(self, X_processed, labels, feature_cols):
        """绘制聚类结果图表"""
        # 使用PCA降维到2D进行可视化
        if X_processed.shape[1] > 2:
            pca = PCA(n_components=2)
            X_2d = pca.fit_transform(X_processed)
            explained_var = pca.explained_variance_ratio_
        else:
            X_2d = X_processed
            explained_var = [1.0, 1.0]
        
        # 1. 聚类散点图
        fig, ax = plt.subplots(figsize=(10, 8))
        unique_labels = np.unique(labels)
        colors = plt.cm.tab10(np.linspace(0, 1, len(unique_labels)))
        
        for i, label in enumerate(unique_labels):
            if label == -1:
                # 噪声点
                mask = labels == label
                ax.scatter(X_2d[mask, 0], X_2d[mask, 1], c='gray', 
                          marker='x', s=50, alpha=0.5, label='噪声点')
            else:
                mask = labels == label
                ax.scatter(X_2d[mask, 0], X_2d[mask, 1], c=[colors[i]], 
                          marker='o', s=80, alpha=0.7, edgecolors='white', linewidth=0.5,
                          label=f'簇 {label}')
        
        ax.set_xlabel(f'PC1 ({explained_var[0]*100:.1f}%)', fontsize=12)
        ax.set_ylabel(f'PC2 ({explained_var[1]*100:.1f}%)', fontsize=12)
        ax.set_title('聚类结果可视化 (PCA降维)', fontsize=14, fontweight='bold')
        ax.legend(loc='best', title='簇标签')
        ax.grid(True, alpha=0.3)
        self.figs['cluster_scatter'] = self._fig_to_base64(fig)
        plt.close(fig)
        
        # 2. 簇分布柱状图
        fig, ax = plt.subplots(figsize=(8, 6))
        unique, counts = np.unique(labels, return_counts=True)
        colors_bar = ['gray' if u == -1 else plt.cm.tab10(i) for i, u in enumerate(unique)]
        bars = ax.bar(range(len(unique)), counts, color=colors_bar, edgecolor='white', linewidth=1.5)
        ax.set_xticks(range(len(unique)))
        ax.set_xticklabels([f'噪声' if u == -1 else f'簇 {u}' for u in unique])
        ax.set_xlabel('簇标签', fontsize=12)
        ax.set_ylabel('样本数', fontsize=12)
        ax.set_title('各簇样本分布', fontsize=14, fontweight='bold')
        
        # 添加数值标签
        for bar, count in zip(bars, counts):
            height = bar.get_height()
            ax.text(bar.get_x() + bar.get_width()/2., height,
                   f'{count}',
                   ha='center', va='bottom', fontsize=10, fontweight='bold')
        
        self.figs['cluster_distribution'] = self._fig_to_base64(fig)
        plt.close(fig)
        
        # 3. 肘部法则（仅K-Means）
        if self.model.__class__.__name__ == 'KMeans':
            fig, ax = plt.subplots(figsize=(8, 6))
            inertias = []
            K_range = range(2, 11)
            for k in K_range:
                kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
                kmeans.fit(X_processed)
                inertias.append(kmeans.inertia_)
            
            ax.plot(K_range, inertias, 'bo-', linewidth=2, markersize=8, markerfacecolor='#FF6B6B')
            ax.set_xlabel('聚类数 K', fontsize=12)
            ax.set_ylabel('惯性 (Inertia)', fontsize=12)
            ax.set_title('肘部法则选择最优K值', fontsize=14, fontweight='bold')
            ax.grid(True, alpha=0.3)
            ax.set_xticks(list(K_range))
            self.figs['elbow_method'] = self._fig_to_base64(fig)
            plt.close(fig)
    
    def _fig_to_base64(self, fig) -> str:
        """将matplotlib图表转换为base64字符串"""
        buf = BytesIO()
        fig.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                   facecolor='#ffffff', edgecolor='none')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        buf.close()
        return img_base64
    
    def predict(self, X_new: pd.DataFrame) -> np.ndarray:
        """使用训练好的模型进行预测"""
        if self.model is None:
            raise ValueError("模型尚未训练")
        
        X_processed = self._preprocess_features(X_new, fit=False)
        return self.model.predict(X_processed)
    
    def get_results(self) -> Dict[str, Any]:
        """获取完整的训练结果"""
        return {
            'model_type': self.task_type,
            'metrics': self.metrics,
            'figures': self.figs,
            'feature_cols': self.feature_cols,
            'target_col': self.target_col,
        }


# 如果 xgboost 可用，注册 XGBoost 模型
if HAS_XGBOOST:
    MLTrainer.REGRESSION_MODELS['xgboost_reg'] = {
        'label': 'XGBoost回归', 'class': XGBRegressor,
        'params': {'n_estimators': [50, 100], 'max_depth': [3, 5]}
    }
    MLTrainer.CLASSIFICATION_MODELS['xgboost_clf'] = {
        'label': 'XGBoost分类', 'class': XGBClassifier,
        'params': {'n_estimators': [50, 100], 'max_depth': [3, 5]}
    }


def get_ml_model_list() -> Dict[str, Any]:
    """获取所有可用的机器学习模型列表"""
    return {
        'regression': {
            'title': '回归模型',
            'description': '用于预测连续数值目标变量',
            'models': [
                {'key': 'linear_regression', 'name': '线性回归', 'desc': '简单线性关系建模'},
                {'key': 'ridge', 'name': '岭回归', 'desc': 'L2正则化，防止过拟合'},
                {'key': 'lasso', 'name': 'Lasso回归', 'desc': 'L1正则化，自动特征选择'},
                {'key': 'decision_tree_reg', 'name': '决策树回归', 'desc': '非线性关系，可解释性强'},
                {'key': 'random_forest_reg', 'name': '随机森林回归', 'desc': '集成学习，高精度'},
                {'key': 'gradient_boosting_reg', 'name': '梯度提升回归', 'desc': '逐步优化，性能优秀'},
                {'key': 'svr', 'name': '支持向量回归', 'desc': '适合高维数据'},
                {'key': 'knn_reg', 'name': 'K近邻回归', 'desc': '基于相似样本预测'},
                {'key': 'xgboost_reg', 'name': 'XGBoost回归', 'desc': '极端梯度提升，竞赛常用'},
            ]
        },
        'classification': {
            'title': '分类模型',
            'description': '用于预测离散类别目标变量',
            'models': [
                {'key': 'logistic_regression', 'name': '逻辑回归', 'desc': '基础分类，可解释性强'},
                {'key': 'decision_tree_clf', 'name': '决策树分类', 'desc': '非线性决策边界'},
                {'key': 'random_forest_clf', 'name': '随机森林分类', 'desc': '集成学习，高精度'},
                {'key': 'svm', 'name': '支持向量机', 'desc': '适合高维数据'},
                {'key': 'knn_clf', 'name': 'K近邻分类', 'desc': '基于相似样本分类'},
                {'key': 'naive_bayes', 'name': '朴素贝叶斯', 'desc': '概率分类，适合文本'},
                {'key': 'xgboost_clf', 'name': 'XGBoost分类', 'desc': '极端梯度提升'},
            ]
        },
        'clustering': {
            'title': '聚类模型',
            'description': '无监督学习，发现数据中的自然分组',
            'models': [
                {'key': 'kmeans', 'name': 'K-Means聚类', 'desc': '经典划分聚类'},
                {'key': 'dbscan', 'name': 'DBSCAN聚类', 'desc': '密度聚类，发现任意形状'},
                {'key': 'agglomerative', 'name': '层次聚类', 'desc': '自底向上合并'},
                {'key': 'gmm', 'name': '高斯混合模型', 'desc': '软聚类，概率分配'},
            ]
        }
    }


if __name__ == '__main__':
    # 测试代码
    print("机器学习模块加载成功")
    print("支持的模型:")
    for task, info in get_ml_model_list().items():
        print(f"\n{info['title']}:")
        for m in info['models']:
            print(f"  - {m['name']}: {m['desc']}")
