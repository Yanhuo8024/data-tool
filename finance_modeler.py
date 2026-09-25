import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from sklearn.linear_model import LinearRegression, Ridge, Lasso
from sklearn.ensemble import RandomForestRegressor, GradientBoostingRegressor
from sklearn.neighbors import KNeighborsRegressor
from sklearn.svm import SVR
from sklearn.preprocessing import MinMaxScaler
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
import io
import base64
import warnings
warnings.filterwarnings('ignore')

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'Arial Unicode MS']
plt.rcParams['axes.unicode_minus'] = False

class FinanceModeler:
    """金融数据建模和可视化"""

    def __init__(self, data=None):
        self.data = data
        self.model = None
        self.scaler = MinMaxScaler()
        self.features = None
        self.target = None
        self.predictions = None

    def load_data(self, df):
        """加载金融数据"""
        self.data = df.copy()
        # 尝试识别常见的金融数据列
        self.price_cols = []
        for col in df.columns:
            col_lower = col.lower()
            if any(kw in col_lower for kw in ['close', '收盘', 'price', '价格', 'open', 'high', 'low', 'volume', 'vol', 'amount', '成交额']):
                self.price_cols.append(col)
        return self

    def prepare_features(self, target_col=None, feature_cols=None):
        """准备特征和目标变量"""
        if self.data is None:
            raise ValueError("请先加载数据")

        df = self.data.copy()

        # 自动识别目标列(收盘价优先)
        if target_col is None:
            for col in df.columns:
                if 'close' in col.lower() or '收盘' in col:
                    target_col = col
                    break
            if target_col is None and len(df.columns) > 0:
                target_col = df.columns[-1]

        self.target_col = target_col

        # 选择数值型特征
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()

        if feature_cols is None:
            feature_cols = [c for c in numeric_cols if c != target_col]

        self.feature_cols = feature_cols

        # 处理缺失值
        df = df[feature_cols + [target_col]].dropna()

        self.features = df[feature_cols].values
        self.target = df[target_col].values

        return self

    def add_technical_indicators(self):
        """添加技术指标作为特征"""
        if self.data is None:
            raise ValueError("请先加载数据")

        df = self.data.copy()
        n = len(df)
        numeric_cols = df.select_dtypes(include=[np.number]).columns

        # 找到价格列
        price_col = None
        for col in numeric_cols:
            if 'close' in col.lower() or '收盘' in col:
                price_col = col
                break

        if price_col is None:
            price_col = numeric_cols[0] if len(numeric_cols) > 0 else None

        if price_col is None:
            return self

        # 根据数据量选择合适的窗口大小
        # 移动平均线 - 只添加数据量足够的窗口
        for window in [5, 10, 20, 60]:
            if n >= window + 5:  # 确保有足够的数据
                df[f'MA{window}'] = df[price_col].rolling(window=window).mean()

        # 价格变化率
        df['Returns'] = df[price_col].pct_change()

        # 波动率 - 只在数据量足够时添加
        if n >= 25:
            df['Volatility'] = df['Returns'].rolling(window=20).std()

        # RSI - 只在数据量足够时添加
        if n >= 20:
            delta = df[price_col].diff()
            gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
            rs = gain / loss
            df['RSI'] = 100 - (100 / (1 + rs))

        # MACD - 始终添加（ewm不会产生NaN）
        ema12 = df[price_col].ewm(span=12).mean()
        ema26 = df[price_col].ewm(span=26).mean()
        df['MACD'] = ema12 - ema26
        df['MACD_Signal'] = df['MACD'].ewm(span=9).mean()

        # 布林带 - 只在数据量足够时添加
        if n >= 25:
            df['BB_Middle'] = df[price_col].rolling(window=20).mean()
            bb_std = df[price_col].rolling(window=20).std()
            df['BB_Upper'] = df['BB_Middle'] + (bb_std * 2)
            df['BB_Lower'] = df['BB_Middle'] - (bb_std * 2)

        self.data = df
        return self

    def fund_performance_metrics(self, nav_col=None, rf=0.02, trading_days=252):
        """计算开放基金业绩指标（夏普比率、最大回撤、年化收益等）

        nav_col: 净值列名（自动识别单位净值/净值/close 等）
        rf: 无风险利率（年化，默认 2%）
        trading_days: 年化交易日数（基金按净值日计，默认 252）
        返回: dict 业绩指标 + base64 净值曲线图
        """
        if self.data is None or self.data.empty:
            return {"ok": False, "error": "无数据"}
        df = self.data.copy()

        # 识别净值列（跳过日期列，避免匹配到"净值日期"）
        if nav_col is None:
            for c in df.columns:
                cl = str(c).lower()
                if any(k in cl for k in ('日期', 'date', '时间', 'time', 'index')):
                    continue  # 跳过日期/索引列
                if any(k in cl for k in ('单位净值', '累计净值', 'nav', '净值')):
                    nav_col = c
                    break
        if nav_col is None:
            for c in df.columns:
                if str(c).lower() in ('close', '收盘', 'price', '价格'):
                    nav_col = c
                    break
        if nav_col is None:
            num_cols = df.select_dtypes(include=[np.number]).columns.tolist()
            if num_cols:
                nav_col = num_cols[0]
        if nav_col is None:
            return {"ok": False, "error": "未找到净值列"}

        nav = pd.to_numeric(df[nav_col], errors='coerce').dropna()
        if len(nav) < 2:
            return {"ok": False, "error": "净值数据不足"}

        rets = nav.pct_change().dropna()
        if len(rets) < 2:
            return {"ok": False, "error": "收益率数据不足"}

        # ---- 指标计算 ----
        total_return = float(nav.iloc[-1] / nav.iloc[0] - 1)
        years = len(rets) / trading_days
        annual_return = float((nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1) if years > 0 else total_return
        annual_vol = float(rets.std() * np.sqrt(trading_days))
        sharpe = float((annual_return - rf) / annual_vol) if annual_vol > 0 else 0.0

        # 最大回撤
        cum = nav.cummax()
        drawdown = (nav - cum) / cum
        max_drawdown = float(drawdown.min())

        # 卡玛比率（年化收益 / 最大回撤绝对值）
        calmar = float(annual_return / abs(max_drawdown)) if max_drawdown < 0 else 0.0

        # 胜率与波动
        win_rate = float((rets > 0).mean())

        # 索提诺比率（下行风险）
        downside = rets[rets < 0]
        downside_vol = float(downside.std() * np.sqrt(trading_days)) if len(downside) > 1 else 0.0
        sortino = float((annual_return - rf) / downside_vol) if downside_vol > 0 else 0.0

        metrics = {
            "ok": True,
            "nav_col": nav_col,
            "data_points": int(len(nav)),
            "total_return": round(total_return * 100, 2),      # %
            "annual_return": round(annual_return * 100, 2),    # %
            "annual_volatility": round(annual_vol * 100, 2),   # %
            "sharpe": round(sharpe, 3),
            "sortino": round(sortino, 3),
            "max_drawdown": round(max_drawdown * 100, 2),      # %
            "calmar": round(calmar, 3),
            "win_rate": round(win_rate * 100, 2),              # %
            "risk_free": rf,
        }

        # ---- 净值曲线 + 回撤图 ----
        try:
            import base64 as _b64
            fig, axes = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
            axes[0].plot(nav.index, nav.values, color='#4caf50', lw=1.5)
            axes[0].set_title(f'净值曲线 ({nav_col})  —  累计收益 {total_return*100:.2f}%', fontsize=12)
            axes[0].set_ylabel('净值')
            axes[0].grid(alpha=0.3)
            axes[1].fill_between(drawdown.index, drawdown.values * 100, 0, color='#ef5350', alpha=0.5)
            axes[1].set_title('回撤曲线 (%)', fontsize=10)
            axes[1].set_ylabel('回撤%')
            axes[1].grid(alpha=0.3)
            plt.tight_layout()
            buf = io.BytesIO()
            fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            plt.close(fig)
            metrics["chart"] = "data:image/png;base64," + _b64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass

        return metrics

    def train_model(self, model_type='linear', test_size=0.2):
        """训练模型"""
        if self.features is None or self.target is None:
            self.prepare_features()

        # 划分训练集和测试集
        n = len(self.target)
        split_idx = int(n * (1 - test_size))

        X_train, X_test = self.features[:split_idx], self.features[split_idx:]
        y_train, y_test = self.target[:split_idx], self.target[split_idx:]

        # 标准化
        X_train_scaled = self.scaler.fit_transform(X_train)
        X_test_scaled = self.scaler.transform(X_test)

        # 选择模型
        models = {
            'linear': LinearRegression(),
            'ridge': Ridge(alpha=1.0),
            'lasso': Lasso(alpha=0.1),
            'rf': RandomForestRegressor(n_estimators=100, random_state=42),
            'gb': GradientBoostingRegressor(n_estimators=100, random_state=42),
            'knn': KNeighborsRegressor(n_neighbors=5),
            'svr': SVR(kernel='rbf')
        }

        self.model = models.get(model_type, LinearRegression())
        self.model.fit(X_train_scaled, y_train)

        # 预测
        y_pred = self.model.predict(X_test_scaled)

        # 计算指标
        self.metrics = {
            'rmse': np.sqrt(mean_squared_error(y_test, y_pred)),
            'mae': mean_absolute_error(y_test, y_pred),
            'r2': r2_score(y_test, y_pred),
            'model_name': model_type.upper()
        }

        return self.metrics

    def predict_future(self, days=5):
        """预测未来价格"""
        if self.model is None:
            raise ValueError("请先训练模型")

        last_features = self.features[-1:]
        predictions = []

        for _ in range(days):
            scaled = self.scaler.transform(last_features)
            pred = self.model.predict(scaled)[0]
            predictions.append(pred)
            # 更新特征(简化处理)
            last_features = np.roll(last_features, -1)
            last_features[0, -1] = pred

        self.predictions = predictions
        return predictions

    def create_candlestick_chart(self, df=None, title='K线图', figsize=(12, 8)):
        """创建K线图"""
        if df is None:
            df = self.data

        fig, axes = plt.subplots(2, 1, figsize=figsize, gridspec_kw={'height_ratios': [3, 1]})

        # 识别列
        open_col = next((c for c in df.columns if 'open' in c.lower() or '开盘' in c), None)
        high_col = next((c for c in df.columns if 'high' in c.lower() or '最高' in c), None)
        low_col = next((c for c in df.columns if 'low' in c.lower() or '最低' in c), None)
        close_col = next((c for c in df.columns if 'close' in c.lower() or '收盘' in c), None)
        volume_col = next((c for c in df.columns if 'volume' in c.lower() or 'vol' in c.lower() or '量' in c), None)

        if not all([open_col, high_col, low_col, close_col]):
            # 如果没有OHLC数据,使用线图
            ax = axes[0]
            for col in [close_col, open_col]:
                if col:
                    ax.plot(df.index, df[col], label=col, linewidth=1.5)
            ax.set_title(title, fontsize=14, fontweight='bold')
            ax.set_ylabel('价格', fontsize=12)
            ax.legend(loc='upper left', fontsize=10)
            ax.grid(True, alpha=0.3)
        else:
            # 绘制K线
            ax = axes[0]
            for i in range(len(df)):
                open_p = df[open_col].iloc[i]
                high_p = df[high_col].iloc[i]
                low_p = df[low_col].iloc[i]
                close_p = df[close_col].iloc[i]

                color = 'red' if close_p >= open_p else 'green'

                # 实体
                ax.bar(i, close_p - open_p, bottom=open_p, color=color, width=0.8, edgecolor=color)
                # 影线
                ax.plot([i, i], [low_p, high_p], color=color, linewidth=1)

            ax.set_title(title, fontsize=14, fontweight='bold')
            ax.set_ylabel('价格', fontsize=12)
            ax.set_xticks(range(0, len(df), max(1, len(df)//10)))
            ax.set_xticklabels([str(df.index[i])[:10] for i in range(0, len(df), max(1, len(df)//10))], rotation=45)
            ax.grid(True, alpha=0.3)

        # 成交量
        if volume_col:
            ax2 = axes[1]
            colors = ['red' if df[close_col].iloc[i] >= df[open_col].iloc[i] else 'green' for i in range(len(df))] if open_col else ['blue'] * len(df)
            ax2.bar(range(len(df)), df[volume_col], color=colors, alpha=0.7)
            ax2.set_ylabel('成交量', fontsize=12)
            ax2.set_xlabel('时间', fontsize=12)
            ax2.grid(True, alpha=0.3)

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()

        return img_base64

    def create_ma_chart(self, df=None, title='移动平均线分析', figsize=(12, 6)):
        """创建移动平均线图"""
        if df is None:
            df = self.data

        fig, ax = plt.subplots(figsize=figsize)

        # 找到价格列
        price_col = None
        for col in df.columns:
            if 'close' in col.lower() or '收盘' in col:
                price_col = col
                break

        if price_col is None:
            price_col = df.select_dtypes(include=[np.number]).columns[0]

        # 绘制价格线
        ax.plot(df.index, df[price_col], label='收盘价', linewidth=2, color='black')

        # 绘制移动平均线
        colors = ['#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4']
        for i, window in enumerate([5, 10, 20, 60]):
            ma_col = f'MA{window}'
            if ma_col in df.columns:
                ax.plot(df.index, df[ma_col], label=f'MA{window}', linewidth=1.5, color=colors[i % len(colors)])

        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('时间', fontsize=12)
        ax.set_ylabel('价格', fontsize=12)
        ax.legend(loc='upper left', fontsize=10, framealpha=0.9)
        ax.grid(True, alpha=0.3)

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()

        return img_base64

    def create_macd_chart(self, df=None, title='MACD指标分析', figsize=(12, 8)):
        """创建MACD图"""
        if df is None:
            df = self.data

        fig, axes = plt.subplots(2, 1, figsize=figsize, gridspec_kw={'height_ratios': [2, 1]})

        # 价格线
        price_col = next((c for c in df.columns if 'close' in c.lower() or '收盘' in c), df.select_dtypes(include=[np.number]).columns[0])
        axes[0].plot(df.index, df[price_col], label='收盘价', linewidth=1.5, color='black')
        axes[0].set_title(title, fontsize=14, fontweight='bold')
        axes[0].set_ylabel('价格', fontsize=12)
        axes[0].legend(loc='upper left', fontsize=10)
        axes[0].grid(True, alpha=0.3)

        # MACD
        if 'MACD' in df.columns and 'MACD_Signal' in df.columns:
            axes[1].plot(df.index, df['MACD'], label='MACD', linewidth=1.5, color='blue')
            axes[1].plot(df.index, df['MACD_Signal'], label='信号线', linewidth=1.5, color='red')
            axes[1].axhline(y=0, color='gray', linestyle='--', alpha=0.5)
            axes[1].fill_between(df.index, df['MACD'], df['MACD_Signal'],
                                where=(df['MACD'] >= df['MACD_Signal']),
                                color='red', alpha=0.3, label='多头区域')
            axes[1].fill_between(df.index, df['MACD'], df['MACD_Signal'],
                                where=(df['MACD'] < df['MACD_Signal']),
                                color='green', alpha=0.3, label='空头区域')
            axes[1].set_ylabel('MACD', fontsize=12)
            axes[1].set_xlabel('时间', fontsize=12)
            axes[1].legend(loc='upper left', fontsize=10)
            axes[1].grid(True, alpha=0.3)

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()

        return img_base64

    def create_rsi_chart(self, df=None, title='RSI相对强弱指标', figsize=(12, 8)):
        """创建RSI图"""
        if df is None:
            df = self.data

        fig, axes = plt.subplots(2, 1, figsize=figsize, gridspec_kw={'height_ratios': [2, 1]})

        # 价格线
        price_col = next((c for c in df.columns if 'close' in c.lower() or '收盘' in c), df.select_dtypes(include=[np.number]).columns[0])
        axes[0].plot(df.index, df[price_col], label='收盘价', linewidth=1.5, color='black')
        axes[0].set_title(title, fontsize=14, fontweight='bold')
        axes[0].set_ylabel('价格', fontsize=12)
        axes[0].legend(loc='upper left', fontsize=10)
        axes[0].grid(True, alpha=0.3)

        # RSI
        if 'RSI' in df.columns:
            axes[1].plot(df.index, df['RSI'], label='RSI', linewidth=1.5, color='purple')
            axes[1].axhline(y=70, color='red', linestyle='--', alpha=0.7, label='超买线(70)')
            axes[1].axhline(y=30, color='green', linestyle='--', alpha=0.7, label='超卖线(30)')
            axes[1].fill_between(df.index, 30, 70, alpha=0.1, color='gray', label='正常区域')
            axes[1].set_ylim(0, 100)
            axes[1].set_ylabel('RSI', fontsize=12)
            axes[1].set_xlabel('时间', fontsize=12)
            axes[1].legend(loc='upper left', fontsize=10)
            axes[1].grid(True, alpha=0.3)

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()

        return img_base64

    def create_bollinger_chart(self, df=None, title='布林带分析', figsize=(12, 6)):
        """创建布林带图"""
        if df is None:
            df = self.data

        fig, ax = plt.subplots(figsize=figsize)

        price_col = next((c for c in df.columns if 'close' in c.lower() or '收盘' in c), df.select_dtypes(include=[np.number]).columns[0])

        ax.plot(df.index, df[price_col], label='收盘价', linewidth=1.5, color='black')

        if all(col in df.columns for col in ['BB_Upper', 'BB_Middle', 'BB_Lower']):
            ax.plot(df.index, df['BB_Upper'], label='上轨', linewidth=1, color='red', linestyle='--')
            ax.plot(df.index, df['BB_Middle'], label='中轨', linewidth=1, color='blue', linestyle='-')
            ax.plot(df.index, df['BB_Lower'], label='下轨', linewidth=1, color='green', linestyle='--')
            ax.fill_between(df.index, df['BB_Upper'], df['BB_Lower'], alpha=0.1, color='gray', label='布林带通道')

        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('时间', fontsize=12)
        ax.set_ylabel('价格', fontsize=12)
        ax.legend(loc='upper left', fontsize=10, framealpha=0.9)
        ax.grid(True, alpha=0.3)

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()

        return img_base64

    def create_volume_chart(self, df=None, title='成交量分析', figsize=(12, 6)):
        """创建成交量图"""
        if df is None:
            df = self.data

        fig, ax = plt.subplots(figsize=figsize)

        volume_col = next((c for c in df.columns if 'volume' in c.lower() or 'vol' in c.lower() or '量' in c), None)
        close_col = next((c for c in df.columns if 'close' in c.lower() or '收盘' in c), None)
        open_col = next((c for c in df.columns if 'open' in c.lower() or '开盘' in c), None)

        if volume_col:
            colors = []
            if close_col and open_col:
                for i in range(len(df)):
                    if df[close_col].iloc[i] >= df[open_col].iloc[i]:
                        colors.append('#FF6B6B')
                    else:
                        colors.append('#4ECDC4')
            else:
                colors = ['#45B7D1'] * len(df)

            ax.bar(range(len(df)), df[volume_col], color=colors, alpha=0.7, label='成交量')
            ax.set_title(title, fontsize=14, fontweight='bold')
            ax.set_xlabel('时间', fontsize=12)
            ax.set_ylabel('成交量', fontsize=12)
            ax.legend(loc='upper left', fontsize=10)
            ax.grid(True, alpha=0.3)

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()

        return img_base64

    def create_prediction_chart(self, df=None, predictions=None, title='价格预测', figsize=(12, 6)):
        """创建预测图"""
        if df is None:
            df = self.data
        if predictions is None:
            predictions = self.predictions

        fig, ax = plt.subplots(figsize=figsize)

        price_col = next((c for c in df.columns if 'close' in c.lower() or '收盘' in c), df.select_dtypes(include=[np.number]).columns[0])

        # 历史数据
        hist_dates = range(len(df))
        ax.plot(hist_dates, df[price_col], label='历史价格', linewidth=2, color='black')

        # 预测数据
        if predictions:
            pred_dates = range(len(df), len(df) + len(predictions))
            ax.plot(pred_dates, predictions, label='预测价格', linewidth=2, color='red', linestyle='--')
            ax.scatter(pred_dates, predictions, color='red', s=50, zorder=5, label='预测点')

        ax.axvline(x=len(df)-1, color='gray', linestyle=':', alpha=0.7, label='预测起点')
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xlabel('时间', fontsize=12)
        ax.set_ylabel('价格', fontsize=12)
        ax.legend(loc='upper left', fontsize=10, framealpha=0.9)
        ax.grid(True, alpha=0.3)

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()

        return img_base64

    def create_correlation_heatmap(self, df=None, title='相关性热力图', figsize=(10, 8)):
        """创建相关性热力图"""
        if df is None:
            df = self.data

        numeric_df = df.select_dtypes(include=[np.number])
        corr = numeric_df.corr()

        fig, ax = plt.subplots(figsize=figsize)

        sns.heatmap(corr, annot=True, fmt='.2f', cmap='RdYlBu_r', center=0,
                   square=True, linewidths=0.5, cbar_kws={"shrink": 0.8}, ax=ax)

        ax.set_title(title, fontsize=14, fontweight='bold')

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()

        return img_base64

    def create_returns_distribution(self, df=None, title='收益率分布', figsize=(12, 6)):
        """创建收益率分布图"""
        if df is None:
            df = self.data
        
        fig, axes = plt.subplots(1, 2, figsize=figsize)
        
        price_col = next((c for c in df.columns if 'close' in c.lower() or '收盘' in c), df.select_dtypes(include=[np.number]).columns[0])
        returns = df[price_col].pct_change().dropna()
        
        # 直方图
        axes[0].hist(returns, bins=50, color='steelblue', alpha=0.7, edgecolor='black')
        axes[0].axvline(x=returns.mean(), color='red', linestyle='--', linewidth=2, label=f'均值: {returns.mean():.4f}')
        axes[0].axvline(x=returns.median(), color='green', linestyle='--', linewidth=2, label=f'中位数: {returns.median():.4f}')
        axes[0].set_title(title + ' - 直方图', fontsize=12, fontweight='bold')
        axes[0].set_xlabel('收益率', fontsize=10)
        axes[0].set_ylabel('频数', fontsize=10)
        axes[0].legend(fontsize=9)
        axes[0].grid(True, alpha=0.3)
        
        # Q-Q图
        from scipy import stats
        stats.probplot(returns, dist="norm", plot=axes[1])
        axes[1].set_title('Q-Q图 (正态性检验)', fontsize=12, fontweight='bold')
        axes[1].grid(True, alpha=0.3)
        
        plt.tight_layout()
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()
        
        return img_base64

    def create_bar_chart(self, df=None, title='各列数值分布（柱状图）', figsize=(12, 8)):
        """创建柱状图 - 各列数值分布"""
        if df is None:
            df = self.data
        
        fig, ax = plt.subplots(figsize=figsize)
        
        numeric_cols = df.select_dtypes(include=[np.number]).columns[:8]
        n_cols = len(numeric_cols)
        if n_cols == 0:
            fig.text(0.5, 0.5, '无数值列', ha='center', va='center', fontsize=14)
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            plt.close()
            return img_base64
        
        stats = df[numeric_cols].agg(['mean', 'median', 'max', 'min'])
        x = np.arange(len(numeric_cols))
        width = 0.2
        
        ax.bar(x - 1.5*width, stats.loc['mean'], width, label='均值', color='#FF6B6B', alpha=0.8)
        ax.bar(x - 0.5*width, stats.loc['median'], width, label='中位数', color='#8b5cf6', alpha=0.8)
        ax.bar(x + 0.5*width, stats.loc['max'], width, label='最大值', color='#06b6d4', alpha=0.8)
        ax.bar(x + 1.5*width, stats.loc['min'], width, label='最小值', color='#ef4444', alpha=0.8)
        
        ax.set_xlabel('字段', fontsize=12)
        ax.set_ylabel('数值', fontsize=12)
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xticks(x)
        ax.set_xticklabels(numeric_cols, rotation=45, ha='right')
        ax.legend(fontsize=10)
        ax.grid(axis='y', alpha=0.3)
        
        plt.tight_layout()
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()
        
        return img_base64

    def create_box_plot(self, df=None, title='箱线图 - 异常值检测', figsize=(12, 8)):
        """创建箱线图 - 异常值检测"""
        if df is None:
            df = self.data
        
        fig, ax = plt.subplots(figsize=figsize)
        
        numeric_cols = df.select_dtypes(include=[np.number]).columns[:8]
        if len(numeric_cols) == 0:
            ax.text(0.5, 0.5, '无数值列', ha='center', va='center', fontsize=14)
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            plt.close()
            return img_base64
        
        bp = ax.boxplot([df[c].dropna() for c in numeric_cols], labels=numeric_cols, patch_artist=True)
        
        colors = ['#FF6B6B', '#8b5cf6', '#06b6d4', '#ef4444', '#22c55e', '#f59e0b', '#ec4899', '#14b8a6']
        for patch, color in zip(bp['boxes'], colors):
            patch.set_facecolor(color)
            patch.set_alpha(0.6)
        
        ax.set_xlabel('字段', fontsize=12)
        ax.set_ylabel('数值', fontsize=12)
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.set_xticklabels(numeric_cols, rotation=45, ha='right')
        ax.grid(axis='y', alpha=0.3)
        
        plt.tight_layout()
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()
        
        return img_base64

    def create_density_plot(self, df=None, title='密度图 - 数值分布', figsize=(10, 7)):
        """创建密度图 - 数值分布"""
        if df is None:
            df = self.data
        
        fig, ax = plt.subplots(figsize=figsize)
        
        numeric_cols = df.select_dtypes(include=[np.number]).columns[:5]
        if len(numeric_cols) == 0:
            ax.text(0.5, 0.5, '无数值列', ha='center', va='center', fontsize=14)
            buf = io.BytesIO()
            plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0)
            img_base64 = base64.b64encode(buf.read()).decode('utf-8')
            plt.close()
            return img_base64
        
        colors = ['#FF6B6B', '#8b5cf6', '#06b6d4', '#ef4444', '#22c55e']
        for i, col in enumerate(numeric_cols):
            data = df[col].dropna()
            if len(data) > 1:
                from scipy import stats as scipy_stats
                kde = scipy_stats.gaussian_kde(data)
                x_range = np.linspace(data.min(), data.max(), 200)
                ax.plot(x_range, kde(x_range), color=colors[i % len(colors)], linewidth=2, label=f'{col} (μ={data.mean():.2f}, σ={data.std():.2f})')
                ax.fill_between(x_range, kde(x_range), alpha=0.2, color=colors[i % len(colors)])
        
        ax.set_xlabel('数值', fontsize=12)
        ax.set_ylabel('概率密度', fontsize=12)
        ax.set_title(title, fontsize=14, fontweight='bold')
        ax.legend(fontsize=10, loc='upper right')
        ax.grid(alpha=0.3)
        
        plt.tight_layout()
        
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()
        
        return img_base64

    def create_model_comparison_chart(self, df=None, title='模型性能对比', figsize=(12, 6)):
        """创建模型对比图"""
        models = ['linear', 'ridge', 'lasso', 'rf', 'gb', 'knn', 'svr']
        results = []

        for model_type in models:
            try:
                metrics = self.train_model(model_type=model_type)
                results.append({
                    'model': model_type.upper(),
                    'rmse': metrics['rmse'],
                    'mae': metrics['mae'],
                    'r2': metrics['r2']
                })
            except Exception:
                # 单个模型训练/评估失败时跳过，继续尝试其他模型
                continue

        if not results:
            return None

        results_df = pd.DataFrame(results)

        fig, axes = plt.subplots(1, 3, figsize=figsize)

        # RMSE
        axes[0].bar(results_df['model'], results_df['rmse'], color='coral', alpha=0.8)
        axes[0].set_title('RMSE (越小越好)', fontsize=12, fontweight='bold')
        axes[0].set_ylabel('RMSE', fontsize=10)
        axes[0].tick_params(axis='x', rotation=45)
        axes[0].grid(True, alpha=0.3, axis='y')

        # MAE
        axes[1].bar(results_df['model'], results_df['mae'], color='skyblue', alpha=0.8)
        axes[1].set_title('MAE (越小越好)', fontsize=12, fontweight='bold')
        axes[1].set_ylabel('MAE', fontsize=10)
        axes[1].tick_params(axis='x', rotation=45)
        axes[1].grid(True, alpha=0.3, axis='y')

        # R2
        axes[2].bar(results_df['model'], results_df['r2'], color='lightgreen', alpha=0.8)
        axes[2].set_title('R2 (越接近1越好)', fontsize=12, fontweight='bold')
        axes[2].set_ylabel('R2', fontsize=10)
        axes[2].tick_params(axis='x', rotation=45)
        axes[2].grid(True, alpha=0.3, axis='y')

        plt.suptitle(title, fontsize=14, fontweight='bold', y=1.02)
        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=100, bbox_inches='tight')
        buf.seek(0)
        img_base64 = base64.b64encode(buf.read()).decode('utf-8')
        plt.close()

        return img_base64

    def get_all_charts(self, df=None):
        """获取所有图表"""
        if df is None:
            df = self.data

        charts = []

        # K线图
        try:
            charts.append({'name': 'K线图 (含成交量)', 'data': self.create_candlestick_chart(df)})
        except Exception as e:
            print(f"K线图生成失败: {e}")

        # 移动平均线
        try:
            charts.append({'name': '移动平均线 (MA)', 'data': self.create_ma_chart(df)})
        except Exception as e:
            print(f"MA图生成失败: {e}")

        # MACD
        try:
            charts.append({'name': 'MACD指标', 'data': self.create_macd_chart(df)})
        except Exception as e:
            print(f"MACD图生成失败: {e}")

        # RSI
        try:
            charts.append({'name': 'RSI相对强弱指标', 'data': self.create_rsi_chart(df)})
        except Exception as e:
            print(f"RSI图生成失败: {e}")

        # 布林带
        try:
            charts.append({'name': '布林带', 'data': self.create_bollinger_chart(df)})
        except Exception as e:
            print(f"布林带图生成失败: {e}")

        # 成交量
        try:
            charts.append({'name': '成交量分析', 'data': self.create_volume_chart(df)})
        except Exception as e:
            print(f"成交量图生成失败: {e}")

        # 相关性热力图
        try:
            charts.append({'name': '相关性热力图', 'data': self.create_correlation_heatmap(df)})
        except Exception as e:
            print(f"热力图生成失败: {e}")

        # 收益率分布
        try:
            charts.append({'name': '收益率分布', 'data': self.create_returns_distribution(df)})
        except Exception as e:
            print(f"收益率分布图生成失败: {e}")

        # 柱状图
        try:
            charts.append({'name': '柱状图(各列数值分布)', 'data': self.create_bar_chart(df)})
        except Exception as e:
            print(f"柱状图生成失败: {e}")

        # 箱线图
        try:
            charts.append({'name': '箱线图(异常值检测)', 'data': self.create_box_plot(df)})
        except Exception as e:
            print(f"箱线图生成失败: {e}")

        # 密度图
        try:
            charts.append({'name': '密度图(数值分布)', 'data': self.create_density_plot(df)})
        except Exception as e:
            print(f"密度图生成失败: {e}")

        return charts


# 便捷函数
def analyze_finance_data(df, model_type='linear', predict_days=5):
    """分析金融数据的便捷函数"""
    modeler = FinanceModeler()
    modeler.load_data(df)
    modeler.add_technical_indicators()

    # 训练模型
    metrics = modeler.train_model(model_type=model_type)

    # 预测
    predictions = modeler.predict_future(days=predict_days)

    # 获取所有图表
    charts = modeler.get_all_charts()

    # 预测图
    pred_chart = modeler.create_prediction_chart(predictions=predictions)
    charts.append({'name': '价格预测', 'data': pred_chart})

    return {
        'metrics': metrics,
        'predictions': predictions,
        'figures': charts
    }
