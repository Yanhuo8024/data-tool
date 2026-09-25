#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core_config.py - shared imports, configuration, and utility code for data.tool
"""

import os, sys, json, time, threading, io, traceback, glob, re
import logging
from typing import Any, Dict, Optional
from pathlib import Path
from functools import wraps
from flask import jsonify, request

# ==================== 结构化日志 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('data.tool')

# ==================== 稳定性模块 ====================
from stability import (
    paginator, data_limiter, degradation,
    TimeoutController, RetryHandler, StreamingResponse as Streamer,
    StabilityConfig
)

# ==================== OUT_DIR / UPLOAD_DIR 顶层定义 ====================
# 重要：这两个符号必须在 scraper / rag_unified 之前顶层定义。
# 否则 stock_screener.py 等模块通过 `from core_config import OUT_DIR` 触发懒加载，
# 懒加载会反过来 import scraper，而 scraper.py 内部又是 `from core_config import OUT_DIR`，
# 形成循环 → 部分初始化 → OUT_DIR = None → mkdir 报错。
# 因此 OUT_DIR / UPLOAD_DIR 必须直接定义在此（不走懒加载）。
import os as _os
_OUT_DIR_ENV = _os.environ.get('DATATOOL_OUT_DIR')
if _OUT_DIR_ENV:
    OUT_DIR = Path(_OUT_DIR_ENV)
else:
    OUT_DIR = Path(__file__).resolve().parent / 'output'
OUT_DIR.mkdir(parents=True, exist_ok=True)

_UPLOAD_DIR_ENV = _os.environ.get('DATATOOL_UPLOAD_DIR')
if _UPLOAD_DIR_ENV:
    UPLOAD_DIR = Path(_UPLOAD_DIR_ENV)
else:
    UPLOAD_DIR = OUT_DIR / 'uploads'
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
del _os, _OUT_DIR_ENV, _UPLOAD_DIR_ENV

# ==================== 懒加载注册表 (PEP 562) ====================
# 这些模块在 U盘/冷启动上加载代价高（matplotlib 子链 + akshare/tushare/scipy/sklearn/duckdb/prophet 等）
# 通过模块级 __getattr__ 实现延迟加载：第一次访问符号时才真正 import。
# 外部 `from core_config import X` 与 `core_config.X` 都能命中 —— 但这**仅对 __dict__ 中
# 没有占位符的符号成立**。已被占位符占位的符号（HAS_* / MLTrainer / ALGORITHM_CATALOG 等）
# 属性查找会「成功」，__getattr__ 不触发，from-import 只会拿到占位值（False/None/{}）
# 且永不更新。读取这些符号请用文件末尾的 has_*() / get_*() / ml_model_list()。
# 副作用：HAS_* 标志在第一次访问相应符号时才确定；不访问就不付出 import 代价。
# 注意：OUT_DIR / UPLOAD_DIR 不在此表——已在上面顶层定义以避免 scraper/rag_unified 循环依赖。
_LAZY_IMPORTS = {
    # module_name : [list of attribute names exposed at this module level]
    'rag_unified': ['rag_engine'],  # UPLOAD_DIR 已在上面顶层定义
    'task_queue': ['task_queue'],
    'secure_config': ['get_tushare_token'],
    'sandbox_exec': ['safe_formula_eval', 'validate_upload_file', 'SandboxError'],
    'secure_safe_load': ['safe_load'],
    'finance_data': ['get_kline'],
    'benchmark': ['compute_benchmark_metrics', 'get_benchmark_returns', 'INDICES'],
    'pipeline': ['pipeline_store', 'PipelineExecutor', 'NODE_INFO'],
    'monitoring': ['init_monitoring', 'memory_monitor', 'task_tracker', 'get_system_health', 'get_recent_errors'],
    'nlq_router': ['nlq_execute', 'nlq_query'],
    'table_tools': ['multi_table_join', 'FINANCE_FORMULAS', 'apply_formula',
                    'create_pivot', 'create_crosstab', 'random_sample',
                    'stratified_sample', 'time_segment_sample', 'get_file_columns'],
    'timeseries_tools': ['extract_date_features', 'resample_timeseries', 'prophet_forecast',
                          'rolling_window_calc', 'get_numeric_columns', 'decompose_timeseries',
                          'adf_test_and_diff', 'arima_sarima_model'],
    'scraper': ['Scraper', 'setup_logging', 'load_raw', 'diagnose', 'clean', 'analyze', 'Storage'],  # OUT_DIR 已在上面顶层定义
}
# 反向索引：symbol -> module_name
_LAZY_SYMBOL_TO_MODULE = {
    sym: mod_name
    for mod_name, attrs in _LAZY_IMPORTS.items()
    for sym in attrs
}


def _lazy_load(module_name: str, attr_name: str):
    """实际执行 import。失败时缓存 None。"""
    try:
        import importlib
        mod = importlib.import_module(module_name)
        val = getattr(mod, attr_name)
    except Exception as e:
        logger.warning(f'Lazy import failed: {module_name}.{attr_name} -> {e}')
        val = None
    globals()[attr_name] = val
    return val


def __getattr__(name: str):
    """PEP 562 module-level __getattr__ — 在 `from core_config import X` 时被调用。
    优先处理触发器（finance_quant / finviz_advanced），再到普通 lazy 注册表。"""
    if name in _LAZY_TRIGGERS:
        _LAZY_TRIGGERS[name]()  # 触发 _ensure_* 一次性加载
        return globals().get(name)
    mod_name = _LAZY_SYMBOL_TO_MODULE.get(name)
    if mod_name is not None:
        return _lazy_load(mod_name, name)
    raise AttributeError(f"module 'core_config' has no attribute {name!r}")


# ==================== pandas/numpy 必须保留（基础设施，几乎每个模块都用） ====================
import pandas as pd
import numpy as np

# ==================== 中文字体配置 ====================
import matplotlib
matplotlib.use('Agg')
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt

_font_dir = 'C:/Windows/Fonts'
if os.path.exists(_font_dir):
    for _f in os.listdir(_font_dir):
        if _f.endswith('.ttf'):
            try:
                fm.fontManager.addfont(os.path.join(_font_dir, _f))
            except Exception:
                pass
plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'PingFang SC', 'Noto Sans SC']
plt.rcParams['axes.unicode_minus'] = False

# ==================== 金融量化分析模块 (lazy via trigger) ====================
# 这些模块顶层 import matplotlib + plotly + sklearn + benchmark；冷启动代价高。
# 通过 _LAZY_TRIGGERS 延迟到首次访问符号时再 import。
# 注意: HAS_FIN_MODERS/HAS_ADV_VIZ 保持为普通模块变量（property 在 module-level 无效）。
_LAZY_TRIGGERS = {}  # symbol_name -> ensure_function
HAS_FIN_MODELS = False  # 触发 _ensure_fin_models 后会被覆盖为 True
HAS_ADV_VIZ = False

def _ensure_fin_models():
    """第一次访问 finance_quant 符号时才真正 import（含 sklearn + plotly）"""
    global HAS_FIN_MODELS
    if HAS_FIN_MODELS:
        return
    try:
        from finance_quant import (
            capm_analysis, backtest_ma_crossover, backtest_macd,
            evaluate_predictions, list_saved_models, load_model,
            predict_with_model, delete_model, set_model_dir,
        )
        # 注入 globals 让后续访问直接命中（也满足 `from core_config import X`）
        g = globals()
        g['capm_analysis'] = capm_analysis
        g['backtest_ma_crossover'] = backtest_ma_crossover
        g['backtest_macd'] = backtest_macd
        g['evaluate_predictions'] = evaluate_predictions
        g['list_saved_models'] = list_saved_models
        g['load_model'] = load_model
        g['predict_with_model'] = predict_with_model
        g['delete_model'] = delete_model
        g['set_model_dir'] = set_model_dir
        # 设置金融模型保存目录
        try:
            set_model_dir(str(Path(__file__).parent / 'output' / 'models'))
        except Exception:
            pass
        HAS_FIN_MODELS = True
    except Exception as e:
        logger.warning(f'finance_quant lazy import failed: {e}')

# 注册触发器
for _n in ('capm_analysis', 'backtest_ma_crossover', 'backtest_macd',
           'evaluate_predictions', 'list_saved_models', 'load_model',
           'predict_with_model', 'delete_model', 'set_model_dir'):
    _LAZY_TRIGGERS[_n] = _ensure_fin_models


# ==================== 高级金融可视化 (lazy via trigger) ====================
def _ensure_adv_viz():
    global HAS_ADV_VIZ
    if HAS_ADV_VIZ:
        return
    try:
        from finviz_advanced import (
            money_flow_chart, chip_distribution_chart, interactive_heatmap,
            return_curve_chart, max_drawdown_chart, comparison_chart,
            batch_export_charts, get_available_stocks,
        )
        g = globals()
        g['money_flow_chart'] = money_flow_chart
        g['chip_distribution_chart'] = chip_distribution_chart
        g['interactive_heatmap'] = interactive_heatmap
        g['return_curve_chart'] = return_curve_chart
        g['max_drawdown_chart'] = max_drawdown_chart
        g['comparison_chart'] = comparison_chart
        g['batch_export_charts'] = batch_export_charts
        g['get_available_stocks'] = get_available_stocks
        HAS_ADV_VIZ = True
    except Exception as e:
        logger.warning(f'finviz_advanced lazy import failed: {e}')

for _n in ('money_flow_chart', 'chip_distribution_chart', 'interactive_heatmap',
           'return_curve_chart', 'max_drawdown_chart', 'comparison_chart',
           'batch_export_charts', 'get_available_stocks'):
    _LAZY_TRIGGERS[_n] = _ensure_adv_viz

# ==================== 机器学习增强模块 ====================
try:
    from ml_enhanced import (
        ALGORITHM_CATALOG, auto_feature_engineering, generate_evaluation_panel,
    )
    HAS_ML_ENHANCED = True
except ImportError:
    ALGORITHM_CATALOG = {}
    auto_feature_engineering = generate_evaluation_panel = None
    HAS_ML_ENHANCED = False

# ==================== 数据清洗增强模块 ====================
try:
    from clean_advanced import (
        get_logger, reset_logger, clean_text_column, encode_categorical,
        standardize_dates, batch_replace, condition_filter, missing_heatmap,
    )
    HAS_CLEAN_ADV = True
except ImportError:
    get_logger = reset_logger = clean_text_column = encode_categorical = None
    standardize_dates = batch_replace = condition_filter = missing_heatmap = None
    HAS_CLEAN_ADV = False

# 全局清洗日志缓存（按文件路径）
CLEAN_LOGGERS = {}

# ==================== 配置 ====================
# 绑定 127.0.0.1：仅本机访问，统一用 http://127.0.0.1:5000，
# 不暴露局域网 IP（如需局域网/手机访问再改回 0.0.0.0 并通过防火墙放行）。
HOST = os.environ.get("FLASK_HOST", "127.0.0.1")
PORT = int(os.environ.get("FLASK_PORT", "5000"))
DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

# UTF-8 输出（Windows GBK 兼容）
# 注意：仅在直接运行（非 pytest 捕获环境）且尚未为 UTF-8 时劫持 stdout/stderr，
# 否则会破坏 pytest 的输出捕获导致 "I/O operation on closed file"，也会二次包装已配置好的流。
if ('pytest' not in sys.modules
        and hasattr(sys.stdout, 'buffer')
        and not str(getattr(sys.stdout, 'encoding', '')).lower().startswith('utf')):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except (AttributeError, ValueError, OSError):
        pass

# ==================== Table Tools ====================
from table_tools import (
    multi_table_join, FINANCE_FORMULAS, apply_formula,
    create_pivot, create_crosstab, random_sample,
    stratified_sample, time_segment_sample, get_file_columns,
)

# ==================== Timeseries Tools ====================
from timeseries_tools import (
    extract_date_features, resample_timeseries, prophet_forecast,
    rolling_window_calc, get_numeric_columns,
    decompose_timeseries, adf_test_and_diff, arima_sarima_model,
)

# ==================== 财务报表分析模块 ====================
try:
    from financial_statement import (
        fetch_financial_data, calculate_ratios, generate_charts,
    )
    HAS_FIN_STATEMENT = True
except ImportError:
    fetch_financial_data = calculate_ratios = generate_charts = None
    HAS_FIN_STATEMENT = False

# ==================== 估值分析模块（DCF / 可比公司） ====================
try:
    from valuation import dcf_valuation, peer_comparison
    HAS_VALUATION = True
except ImportError:
    dcf_valuation = peer_comparison = None
    HAS_VALUATION = False

# ==================== 股票筛选器模块 ====================
try:
    from stock_screener import fetch_all_stocks, screen_stocks, export_csv, get_industries
    HAS_SCREENER = True
except ImportError:
    fetch_all_stocks = screen_stocks = export_csv = get_industries = None
    HAS_SCREENER = False

# ==================== NLP 文本分析模块 ====================
try:
    from nlp_tools import (segment_and_count, generate_wordcloud, extract_keywords_tfidf,
                           sentiment_analysis, clean_chinese_text, lda_topics, text_similarity)
    HAS_NLP = True
except ImportError:
    segment_and_count = generate_wordcloud = extract_keywords_tfidf = None
    sentiment_analysis = clean_chinese_text = None
    lda_topics = text_similarity = None
    HAS_NLP = False

# ==================== 报告生成模块 ====================
try:
    from report_generator import save_report, list_templates
    HAS_REPORT = True
except ImportError:
    save_report = list_templates = None
    HAS_REPORT = False

# ==================== Scraper 模块 ====================
sys.path.insert(0, str(Path(__file__).parent))
from scraper import (
    Scraper, setup_logging, load_raw, diagnose, clean, analyze, Storage, OUT_DIR,
)

# ==================== ML 模块（懒加载 — sklearn 全套仅在首次访问时加载） ====================
# ml_module.py 顶层 import sklearn 8 个子模块 + xgboost + matplotlib + seaborn，是
# 冷启动的最大单一瓶颈（~80MB + 编译 .pyc）。改为 lazy：首次访问 MLTrainer/
# get_ml_model_list 时才真正 exec_module。
_ML_MODULE = None
_ML_ENHANCED_LOADED = False


def _ensure_ml_module():
    """首次访问 MLTrainer / get_ml_model_list / ml_enhanced 符号时才真正加载。"""
    global _ML_MODULE, MLTrainer, get_ml_model_list
    global _ML_ENHANCED_LOADED, HAS_ML_ENHANCED, ALGORITHM_CATALOG
    global auto_feature_engineering, generate_evaluation_panel
    if _ML_MODULE is None:
        try:
            import importlib.util as _ilu
            _spec = _ilu.spec_from_file_location(
                "ml_module", str(Path(__file__).parent / "ml_module.py")
            )
            _mod = _ilu.module_from_spec(_spec)
            sys.modules["ml_module"] = _mod
            _spec.loader.exec_module(_mod)
            _ML_MODULE = _mod
            MLTrainer = _mod.MLTrainer
            get_ml_model_list = _mod.get_ml_model_list
            globals()['MLTrainer'] = MLTrainer
            globals()['get_ml_model_list'] = get_ml_model_list
        except Exception as e:
            logger.warning(f'ml_module lazy import failed: {e}')
            globals()['MLTrainer'] = None
            globals()['get_ml_model_list'] = lambda: []
    if not _ML_ENHANCED_LOADED:
        try:
            from ml_enhanced import (
                ALGORITHM_CATALOG as _ac, auto_feature_engineering as _afe,
                generate_evaluation_panel as _gep,
            )
            globals()['ALGORITHM_CATALOG'] = _ac
            globals()['auto_feature_engineering'] = _afe
            globals()['generate_evaluation_panel'] = _gep
            HAS_ML_ENHANCED = True
            globals()['HAS_ML_ENHANCED'] = True
        except Exception as e:
            globals()['ALGORITHM_CATALOG'] = {}
            globals()['auto_feature_engineering'] = None
            globals()['generate_evaluation_panel'] = None
            HAS_ML_ENHANCED = False
            globals()['HAS_ML_ENHANCED'] = False
            logger.warning(f'ml_enhanced lazy import failed: {e}')
        _ML_ENHANCED_LOADED = True

# 占位（首次访问符号前保持轻量）
MLTrainer = None
get_ml_model_list = lambda: []
HAS_ML_ENHANCED = False
ALGORITHM_CATALOG = {}
auto_feature_engineering = None
generate_evaluation_panel = None

for _n in ('MLTrainer', 'get_ml_model_list', 'HAS_ML_ENHANCED',
           'ALGORITHM_CATALOG', 'auto_feature_engineering', 'generate_evaluation_panel'):
    _LAZY_TRIGGERS[_n] = _ensure_ml_module


# ==================== ⚠️ 惰性符号的正确读取方式（2026-09-17 修复） ====================
# 陷阱：上面的占位符已写入本模块 __dict__，而 PEP 562 的模块级 __getattr__
#   只在「属性查找失败」时才被调用 —— 占位符存在 ⇒ 触发器永不执行。
#   于是 `from core_config import HAS_FIN_MODELS` 拿到的是**导入瞬间的 False**；
#   且 from-import 绑定的是「值」而非引用，之后永不更新。
#   历史事故（同源）：
#     - finance_routes 顶层快照 HAS_FIN_MODELS/HAS_ADV_VIZ=False → 9 个端点永久
#       返回「模块未加载」，尽管 finance_quant / finviz_advanced 实际可用；
#     - ml_routes 裸用 HAS_ML_ENHANCED 等符号 → NameError → /api/ml/catalog 500；
#     - /api/ml/models 命中 get_ml_model_list 占位符 → 恒返回 []（静默错值）。
# 正确做法：导入下面这些【函数】（调用时才取值，不会被快照），在判定处调用它们。
def _lazy_flag(name, ensure) -> bool:
    """触发 ensure() 后返回该标志的最新布尔值（幂等）。"""
    try:
        ensure()
    except Exception as e:
        logger.warning(f'lazy flag {name} ensure failed: {e}')
    return bool(globals().get(name))


def has_fin_models() -> bool:
    """金融量化模块（finance_quant）是否可用；首次调用触发懒加载。"""
    return _lazy_flag('HAS_FIN_MODELS', _ensure_fin_models)


def has_adv_viz() -> bool:
    """高级可视化（finviz_advanced）是否可用；首次调用触发懒加载。"""
    return _lazy_flag('HAS_ADV_VIZ', _ensure_adv_viz)


def has_ml_enhanced() -> bool:
    """ml_enhanced 是否可用；首次调用触发懒加载（载入 sklearn 全套，勿在启动期调用）。"""
    return _lazy_flag('HAS_ML_ENHANCED', _ensure_ml_module)


def get_algorithm_catalog() -> dict:
    """ML 算法目录；首次调用触发懒加载。"""
    _ensure_ml_module()
    return globals().get('ALGORITHM_CATALOG') or {}


def get_ml_trainer():
    """MLTrainer 类；首次调用触发懒加载。不可用时返回 None（调用方必须判空）。"""
    _ensure_ml_module()
    return globals().get('MLTrainer')


def ml_model_list() -> list:
    """可用 ML 模型清单；首次调用触发懒加载。异常时返回空列表。"""
    _ensure_ml_module()
    try:
        fn = globals().get('get_ml_model_list')
        return list(fn() or []) if callable(fn) else []
    except Exception as e:
        logger.warning(f'ml_model_list failed: {e}')
        return []


def get_feature_engineering():
    """auto_feature_engineering 函数；首次调用触发懒加载。不可用时返回 None。"""
    _ensure_ml_module()
    return globals().get('auto_feature_engineering')


def get_evaluation_panel():
    """generate_evaluation_panel 函数；首次调用触发懒加载。不可用时返回 None。"""
    _ensure_ml_module()
    return globals().get('generate_evaluation_panel')


# 设置金融模型保存目录（仅在已加载 finance_quant 后 — 现在默认未加载，留给 _ensure_fin_models）
# if HAS_FIN_MODELS: set_model_dir(...) 已迁到 _ensure_fin_models 内部


# ==================== DataFrame 内存缓存（增强版） ====================
CACHE_DISK_DIR = Path(__file__).parent / "output" / "data_cache"

class DataFrameCache:
    """LRU cache for DataFrames with memory limit and file change detection
    增强：可配置容量（env DATAFRAME_CACHE_MAXSIZE，默认 32）+ 磁盘二级缓存（output/data_cache）"""
    
    def __init__(self, max_size=None, memory_limit_mb=2048, disk_cache=True):
        try:
            max_size = int(os.getenv("DATAFRAME_CACHE_MAXSIZE", "32"))
        except (TypeError, ValueError):
            max_size = 32
        self._cache: Dict[str, 'pd.DataFrame'] = {}
        self._access_time: Dict[str, float] = {}
        self._file_mtime: Dict[str, float] = {}
        self._max_size = max_size
        self._memory_limit_mb = memory_limit_mb
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        # 磁盘二级缓存
        self._disk_cache = disk_cache
        self._disk_map: Dict[str, str] = {}  # file_path -> disk key
        if disk_cache:
            try:
                CACHE_DISK_DIR.mkdir(parents=True, exist_ok=True)
            except OSError:
                self._disk_cache = False

    def _disk_key(self, file_path: str) -> str:
        mtime = os.path.getmtime(file_path) if os.path.exists(file_path) else time.time()
        import hashlib
        return hashlib.md5(f"{file_path}|{mtime}".encode()).hexdigest()

    def _disk_load(self, file_path: str) -> Optional['pd.DataFrame']:
        if not self._disk_cache:
            return None
        key = self._disk_key(file_path)
        p = CACHE_DISK_DIR / f"{key}.pkl"
        if p.exists():
            try:
                return pd.read_pickle(str(p))
            except Exception:
                try:
                    p.unlink()
                except OSError:
                    pass
        return None

    def _disk_store(self, file_path: str, df: 'pd.DataFrame'):
        if not self._disk_cache:
            return
        mem_mb = df.memory_usage(deep=True).sum() / (1024 * 1024)
        if mem_mb > 80:
            return  # 仅对中小数据落盘，避免 IO 放大
        key = self._disk_key(file_path)
        self._disk_map[file_path] = key
        p = CACHE_DISK_DIR / f"{key}.pkl"
        try:
            df.to_pickle(str(p), protocol=4)
        except Exception:
            pass

    def _disk_drop(self, file_path: str):
        if not self._disk_cache:
            return
        key = self._disk_map.pop(file_path, None)
        if key:
            try:
                (CACHE_DISK_DIR / f"{key}.pkl").unlink()
            except OSError:
                pass

    def _get_cache_memory_mb(self) -> float:
        total = 0
        for df in self._cache.values():
            total += df.memory_usage(deep=True).sum()
        return total / (1024 * 1024)
    
    def get(self, file_path: str) -> Optional['pd.DataFrame']:
        with self._lock:
            if file_path in self._cache:
                try:
                    file_mtime = os.path.getmtime(file_path)
                    if file_mtime <= self._file_mtime.get(file_path, 0):
                        self._hits += 1
                        self._access_time[file_path] = time.time()
                        return self._cache[file_path]
                    else:
                        for d in [self._cache, self._access_time, self._file_mtime]:
                            d.pop(file_path, None)
                except OSError:
                    pass
            # 内存 miss → 尝试磁盘二级缓存
            disk = self._disk_load(file_path)
            if disk is not None:
                self._hits += 1
                self._access_time[file_path] = time.time()
                self._cache[file_path] = disk  # 提升到内存
                return disk
            self._misses += 1
            return None
    
    def put(self, file_path: str, df: 'pd.DataFrame'):
        with self._lock:
            mem_mb = df.memory_usage(deep=True).sum() / (1024 * 1024)
            if mem_mb > 500:
                logger.warning(f"DataFrame {file_path} ({mem_mb:.0f}MB) too large for cache")
                return
            while len(self._cache) >= self._max_size:
                oldest_key = min(self._access_time, key=self._access_time.get)
                for d in [self._cache, self._access_time, self._file_mtime]:
                    d.pop(oldest_key, None)
            while self._get_cache_memory_mb() > self._memory_limit_mb and self._cache:
                oldest_key = min(self._access_time, key=self._access_time.get)
                for d in [self._cache, self._access_time, self._file_mtime]:
                    d.pop(oldest_key, None)
            self._cache[file_path] = df
            self._access_time[file_path] = time.time()
            try:
                self._file_mtime[file_path] = os.path.getmtime(file_path)
            except OSError:
                self._file_mtime[file_path] = time.time()
            self._disk_store(file_path, df)
    
    def invalidate(self, file_path: str):
        with self._lock:
            for d in [self._cache, self._access_time, self._file_mtime]:
                d.pop(file_path, None)
            self._disk_drop(file_path)
    
    def clear(self):
        with self._lock:
            self._cache.clear()
            self._access_time.clear()
            self._file_mtime.clear()
            self._disk_map.clear()
            if self._disk_cache:
                try:
                    for p in CACHE_DISK_DIR.glob("*.pkl"):
                        p.unlink()
                except OSError:
                    pass
    
    def stats(self) -> Dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._cache),
                "max_size": self._max_size,
                "disk_entries": len(self._disk_map),
                "memory_mb": f"{self._get_cache_memory_mb():.1f}",
                "memory_limit_mb": self._memory_limit_mb,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": f"{self._hits / total * 100:.1f}%" if total > 0 else "N/A",
                "files": list(self._cache.keys())
            }

df_cache = DataFrameCache()


# ==================== 前端资源版本号（替代手写 ?v=） ====================
def compute_asset_ver() -> str:
    """基于 static 下 js/css 最新修改时间生成资源版本号（秒级时间戳）"""
    base = Path(__file__).parent / "static"
    latest = 0.0
    try:
        for p in base.rglob("*"):
            if p.is_file() and p.suffix in ('.js', '.css'):
                latest = max(latest, p.stat().st_mtime)
    except OSError:
        pass
    return str(int(latest)) if latest else "1"


ASSET_VER = compute_asset_ver()


def json_safe(obj):
    """递归将 numpy/pandas 类型转换为 JSON 安全原生类型"""
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


# ==================== 工具函数 ====================

def _load_csv_smart(file_path: str, nrows: Optional[int] = None) -> pd.DataFrame:
    """Smart CSV loading with auto encoding detection"""
    for enc in ['utf-8-sig', 'utf-8', 'gbk', 'gb18030', 'latin1']:
        try:
            return pd.read_csv(file_path, encoding=enc, nrows=nrows, low_memory=True)
        except (UnicodeDecodeError, UnicodeError):
            continue
    # latin1 编码永不失败，作为最终兜底（read_csv 无 errors 参数）
    return pd.read_csv(file_path, encoding='latin1', nrows=nrows, low_memory=True)


def safe_data_path(file_path) -> Path:
    """数据文件路径守卫（P0 安全）：
    - 相对路径锚定 OUT_DIR；
    - 解析后必须仍位于 OUT_DIR 内，否则拒绝（防 `../` 路径穿越读取任意文件，
      如 `file=../data/site.db` 读取站点数据库）。
    合法返回解析后的绝对 Path，非法抛 PermissionError。
    """
    p = Path(file_path)
    if not p.is_absolute():
        p = OUT_DIR / p
    try:
        rp = p.resolve()
    except Exception:
        raise PermissionError(f"非法数据文件路径: {file_path}")
    try:
        inside = rp.is_relative_to(OUT_DIR.resolve())
    except AttributeError:  # 兼容 <3.9
        inside = str(rp).lower().startswith(str(OUT_DIR.resolve()).lower())
    if not inside:
        raise PermissionError(f"数据文件路径越界（仅允许 output/ 目录内文件）: {file_path}")
    return rp


def safe_path_or_resp(raw_path, error_msg=None):
    """safe_data_path 的 Flask 版：合法返回 Path，非法返回 (响应, 403) 元组。

    用法（消除各路由重复的 try/except PermissionError 样板）::

        path = safe_path_or_resp(data["file"])
        if isinstance(path, tuple):
            return path
    """
    try:
        return safe_data_path(raw_path)
    except PermissionError as e:
        return jsonify({"ok": False, "error": error_msg or str(e)}), 403


def load_dataframe(file_path: str, sample_size: Optional[int] = None, force_reload: bool = False) -> pd.DataFrame:
    """Load DataFrame with caching"""
    # P0-1 安全守卫：先做路径穿越校验，再进缓存/读取
    file_path = str(safe_data_path(file_path))

    if not force_reload:
        cached = df_cache.get(file_path)
        if cached is not None:
            if sample_size and len(cached) > sample_size:
                return cached.sample(sample_size, random_state=42)
            return cached

    path = Path(file_path)
    suffix = path.suffix.lower()
    file_size_mb = path.stat().st_size / (1024 * 1024)
    
    if sample_size is None and file_size_mb > 100:
        sample_size = 100000
        logger.info(f"Large file ({file_size_mb:.0f}MB), auto-sampling {sample_size} rows")
    
    if suffix == '.csv':
        # 大文件（>200MB）优先走 DuckDB 列式读取（性能/内存友好），失败降级 pandas
        if file_size_mb > 200:
            try:
                from duckdb_engine import large_load_dataframe
                df = large_load_dataframe(str(path), sample_size)
                if df is not None:
                    logger.info(f"Large CSV via DuckDB: {file_size_mb:.0f}MB (sample={sample_size})")
                else:
                    df = _load_csv_smart(file_path, sample_size)
            except Exception:
                df = _load_csv_smart(file_path, sample_size)
        else:
            df = _load_csv_smart(file_path, sample_size)
    elif suffix in ('.xlsx', '.xls'):
        df = pd.read_excel(file_path, nrows=sample_size)
        if len(df) > 1:
            first_row = df.iloc[0]
            numeric_count = 0
            for val in first_row:
                try:
                    float(val)
                    numeric_count += 1
                except (ValueError, TypeError):
                    pass
            if numeric_count < len(first_row) * 0.3:
                df = df.iloc[1:].reset_index(drop=True)
    elif suffix == '.parquet':
        df = pd.read_parquet(file_path)
        if sample_size and len(df) > sample_size:
            df = df.sample(sample_size, random_state=42)
    else:
        raise ValueError(f"Unsupported format: {suffix}")
    
    for col in df.columns:
        try:
            df[col] = pd.to_numeric(df[col], errors='raise')
        except (ValueError, TypeError):
            pass
    
    df_cache.put(file_path, df)
    return df


def convert_to_parquet(file_path: str) -> str:
    """Convert CSV/Excel to Parquet"""
    path = Path(file_path)
    parquet_path = path.with_suffix('.parquet')
    if parquet_path.exists() and parquet_path.stat().st_mtime > path.stat().st_mtime:
        return str(parquet_path)
    df = load_dataframe(file_path, force_reload=True)
    df.to_parquet(str(parquet_path), index=False, compression='snappy')
    logger.info(f"Converted {file_path} -> {parquet_path}")
    return str(parquet_path)


def handle_errors(f):
    """API error handler decorator"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.error(f"API Error in {f.__name__}: {str(e)}", exc_info=True)
            return jsonify({"error": str(e), "type": type(e).__name__}), 500
    return decorated_function


def _get_csv_file(data, required_cols=None):
    """Get CSV file path (path-traversal safe)"""
    file_name = data.get("file") or data.get("file_name")
    if file_name:
        try:
            path = safe_data_path(file_name)
        except PermissionError:
            return None
        if path.exists():
            return str(path)
        return None
    
    csv_files = sorted(glob.glob(str(OUT_DIR / "*.csv")), key=os.path.getmtime, reverse=True)
    if not csv_files:
        return None
    
    if required_cols:
        for f in csv_files:
            try:
                df = pd.read_csv(f, nrows=0)
                if all(any(c.lower() in col.lower() for col in df.columns) for c in required_cols):
                    return f
            except Exception:
                continue
    
    return csv_files[0]


# ==================== akshare / tushare ====================

def _get_akshare():
    try:
        import akshare as ak
        return ak
    except ImportError:
        return None

def _get_tushare():
    try:
        import tushare as ts
        return ts
    except ImportError:
        return None

_TODAY_YMD = time.strftime('%Y%m%d')
_THIS_YEAR = time.strftime('%Y')


def _latest_report_period(lag_days: int = 45) -> str:
    """最近一个「已披露」的报告期（YYYYMMDD），供三大报表类接口做默认值。

    取当前日期回退 lag_days（默认 45 天，覆盖财报披露滞后）后所属的季度末。
    例：2026-09-14 回退 45 天 → 2026-07-31 → 所属季度 2026Q2 → 20260630。
    """
    import datetime as _d
    ref = _d.date.fromtimestamp(time.time()) - _d.timedelta(days=lag_days)
    q_end = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
    y, q = ref.year, (ref.month - 1) // 3 + 1
    mm, dd = q_end[q]
    if _d.date(y, mm, dd) > ref:
        q -= 1
        if q == 0:
            q, y = 4, y - 1
        mm, dd = q_end[q]
    return f"{y}{mm:02d}{dd:02d}"


_REPORT_PERIOD = _latest_report_period()

# 数据源目录字段约定：
#   label    —— 中文名（前端下拉 / AI 工具描述）
#   desc     —— 一行说明，含关键参数提示
#   cat      —— 归组，供前端 optgroup 与 /api/finance/catalog 使用
#   params   —— 允许注入的 kwarg 名。解析顺序：
#               1) 已知 UI 字段（symbol / period / start_date / end_date / indicator / adjust）
#               2) 请求体 params 字典里的同名键（高级参数透传）
#               3) defaults 中的默认值
#   defaults —— 该接口常量默认值（如报告期）
#   adjust   —— True 时，前端传入的 adjust(qfq/hfq/bfq) 会注入
AK_DATA_FUNCS = {
    # ---------- A股行情 ----------
    "stock_zh_a_spot_em": {"label": "A股实时行情（全市场）", "desc": "沪深京全市场实时快照：价、涨跌幅、量额、PE/PB、市值", "cat": "A股行情"},
    "stock_zh_a_hist": {
        "label": "A股K线（东财，支持复权）",
        "desc": "个股日/周/月K线 (代码, 周期, 起, 止)",
        "cat": "A股行情",
        "params": ["symbol", "period", "start_date", "end_date"],
        "adjust": True,
    },
    "stock_zh_a_daily": {"label": "A股日K（新浪）", "desc": "个股日K，新浪源，支持复权", "cat": "A股行情", "params": ["symbol", "start_date", "end_date"], "adjust": True},
    "stock_zh_a_hist_min_em": {
        "label": "A股分钟K线（1/5/15/30/60）",
        "desc": "个股分钟级K线；周期填 1/5/15/30/60",
        "cat": "A股行情",
        "params": ["symbol", "period", "start_date", "end_date"],
        "adjust": True,
    },
    "stock_individual_info_em": {"label": "个股概况（行业/市值/上市日）", "desc": "个股基本信息：总市值、流通市值、行业、上市时间等", "cat": "A股行情", "params": ["symbol"]},
    "stock_info_a_code_name": {"label": "A股代码名称表", "desc": "全市场A股代码与简称对照表", "cat": "A股行情"},
    "stock_zh_a_st_em": {"label": "风险警示板（ST）", "desc": "ST/退市风险警示股票列表", "cat": "A股行情"},

    # ---------- 港美行情 ----------
    "stock_hk_spot_em": {"label": "港股实时行情（全市场）", "desc": "港股实时快照", "cat": "港美行情"},
    "stock_hk_daily": {"label": "港股日K（新浪，5位代码）", "desc": "港股日K（新浪源, 5位代码如00700, 支持qfq/hfq复权）", "cat": "港美行情", "params": ["symbol"], "adjust": True},
    "stock_hk_hist": {
        "label": "港股K线（东财）",
        "desc": "港股日/周/月K线，代码如 00700",
        "cat": "港美行情",
        "params": ["symbol", "period", "start_date", "end_date"],
        "adjust": True,
    },
    "stock_us_spot_em": {"label": "美股实时行情（全市场）", "desc": "美股实时快照", "cat": "港美行情"},
    "stock_us_hist": {
        "label": "美股K线（东财）",
        "desc": "美股日/周/月K线，代码如 105.MSFT",
        "cat": "港美行情",
        "params": ["symbol", "period", "start_date", "end_date"],
        "adjust": True,
    },
    "stock_us_daily": {"label": "美股日K（新浪）", "desc": "美股日K，代码为简称如 AAPL", "cat": "港美行情", "params": ["symbol"], "adjust": True},

    # ---------- 指数 ----------
    "index_zh_a_hist": {
        "label": "指数K线（沪深）",
        "desc": "指数日/周/月K线，代码如 000300 (symbol, period, start, end)",
        "cat": "指数",
        "params": ["symbol", "period", "start_date", "end_date"],
    },
    "stock_zh_index_daily": {"label": "指数日线（新浪）", "desc": "指数日线，代码带前缀如 sh000001 / sz399001", "cat": "指数", "params": ["symbol"]},
    "stock_zh_index_spot_em": {"label": "指数实时行情", "desc": "指数实时快照；symbol 取「上证系列指数 / 深证系列指数 / 沪深重要指数」", "cat": "指数", "params": ["symbol"], "defaults": {"symbol": "沪深重要指数"}},
    "index_stock_cons": {"label": "指数成分股", "desc": "指数成分股列表，代码如 000300 / 399639", "cat": "指数", "params": ["symbol"]},
    "index_us_stock_sina": {"label": "美股指数", "desc": "美股指数行情，代码如 .INX / .DJI / .IXIC", "cat": "指数", "params": ["symbol"], "defaults": {"symbol": ".INX"}},

    # ---------- 基金 ----------
    "fund_etf_spot_em": {"label": "ETF实时行情（全市场）", "desc": "ETF实时快照", "cat": "基金"},
    "fund_name_em": {"label": "基金代码表", "desc": "全市场基金列表（代码/名称/类型）", "cat": "基金"},
    "fund_open_fund_info_em": {"label": "开放基金历史净值", "desc": "开放基金历史净值 (基金代码, indicator走势)", "cat": "基金", "params": ["symbol", "indicator"]},
    "fund_etf_hist_em": {"label": "ETF历史K线", "desc": "ETF日/周/月K线 (代码, 周期, 起, 止)", "cat": "基金", "params": ["symbol", "period", "start_date", "end_date"], "adjust": True},
    "fund_open_fund_rank_em": {"label": "基金业绩排行", "desc": "开放式基金收益排行；symbol 可取 全部/股票型/混合型/债券型/指数型/QDII", "cat": "基金", "params": ["symbol"], "defaults": {"symbol": "全部"}},
    "fund_portfolio_hold_em": {"label": "基金持仓明细", "desc": "基金重仓持股 (基金代码, 年份)", "cat": "基金", "params": ["symbol", "date"], "defaults": {"date": _THIS_YEAR}},
    "fund_manager_em": {"label": "基金经理列表", "desc": "全市场基金经理基本信息与任职数据", "cat": "基金"},
    "fund_aum_em": {"label": "基金公司规模", "desc": "基金公司管理规模排名", "cat": "基金"},
    "fund_rating_all": {"label": "基金评级", "desc": "全市场基金评级（招商/上海证券等）", "cat": "基金"},

    # ---------- 债券利率 ----------
    "bond_zh_us_rate": {"label": "中美国债收益率", "desc": "中美国债收益率曲线历史（可填起始日）", "cat": "债券利率", "params": ["start_date"]},
    "bond_china_yield": {"label": "中债收益率曲线", "desc": "中债国债/国开债收益率曲线 (起, 止)", "cat": "债券利率", "params": ["start_date", "end_date"]},
    "bond_zh_cov": {"label": "可转债一览", "desc": "全市场可转债列表与转股溢价率", "cat": "债券利率"},
    "bond_zh_hs_cov_daily": {"label": "可转债日线", "desc": "可转债日线，代码带前缀如 sh010107", "cat": "债券利率", "params": ["symbol"]},
    "bond_cb_index_jsl": {"label": "可转债指数（集思录）", "desc": "集思录可转债等权指数历史", "cat": "债券利率"},
    "bond_sh_buy_back_em": {"label": "质押式回购", "desc": "上交所质押式回购成交行情", "cat": "债券利率"},

    # ---------- 外汇 ----------
    "currency_boc_safe": {"label": "人民币汇率中间价", "desc": "国家外汇管理局人民币汇率中间价", "cat": "外汇"},
    "fx_spot_quote": {"label": "外汇即期报价", "desc": "人民币外汇即期报价（CFETS）", "cat": "外汇"},
    "fx_pair_quote": {"label": "外汇掉期报价", "desc": "人民币外汇掉期报价（CFETS）", "cat": "外汇"},

    # ---------- 期货 ----------
    "futures_zh_spot": {"label": "期货实时行情", "desc": "国内期货实时行情 (合约代码, 市场)", "cat": "期货", "params": ["symbol", "market"]},
    "futures_main_sina": {"label": "期货主力连续", "desc": "主力连续合约K线，代码如 RB0 / V0 (起, 止)", "cat": "期货", "params": ["symbol", "start_date", "end_date"]},
    "futures_zh_daily_sina": {"label": "期货日线（新浪）", "desc": "期货日线，代码如 RB0 / IF0", "cat": "期货", "params": ["symbol"]},
    "futures_zh_minute_sina": {"label": "期货分钟线", "desc": "期货分钟线，代码如 IF2008；周期 1/5/15/30/60", "cat": "期货", "params": ["symbol", "period"]},
    "futures_spot_price": {"label": "期货现货价格与基差", "desc": "期货现货价格、基差与展期收益率（date=交易日）", "cat": "期货", "params": ["date"], "defaults": {"date": _TODAY_YMD}},

    # ---------- 市场资金 ----------
    "stock_zt_pool_em": {"label": "涨停板池", "desc": "涨停板个股池（date=交易日，默认当日）", "cat": "市场资金", "params": ["date"], "defaults": {"date": _TODAY_YMD}},
    "stock_lhb_detail_em": {"label": "龙虎榜明细", "desc": "龙虎榜明细（起, 止）", "cat": "市场资金", "params": ["start_date", "end_date"]},
    "stock_board_industry_name_em": {"label": "行业板块一览", "desc": "东财行业板块涨跌与资金流", "cat": "市场资金"},
    "stock_board_concept_name_em": {"label": "概念板块一览", "desc": "东财概念板块涨跌与资金流", "cat": "市场资金"},
    "stock_sector_fund_flow_rank": {"label": "板块资金流向排行", "desc": "行业/概念板块资金流；indicator 取 今日/5日/10日", "cat": "市场资金", "params": ["indicator", "sector_type"], "defaults": {"indicator": "今日", "sector_type": "行业资金流"}},
    "stock_hsgt_fund_flow_summary_em": {"label": "沪深港通资金流向", "desc": "北向/南向资金当日流向汇总", "cat": "市场资金"},
    "stock_margin_sse": {"label": "融资融券（沪市）", "desc": "沪市融资融券汇总（起, 止）", "cat": "市场资金", "params": ["start_date", "end_date"]},
    "stock_margin_szse": {"label": "融资融券（深市）", "desc": "深市融资融券汇总（date=交易日）", "cat": "市场资金", "params": ["date"], "defaults": {"date": _TODAY_YMD}},
    "stock_comment_em": {"label": "千股千评", "desc": "个股综合评分、主力成本、市场关注度", "cat": "市场资金"},
    "stock_restricted_release_queue_em": {"label": "限售解禁明细", "desc": "个股限售股解禁排队（代码）", "cat": "市场资金", "params": ["symbol"]},
    "stock_notice_report": {"label": "公司公告", "desc": "个股公告（代码, date=交易日）", "cat": "市场资金", "params": ["symbol", "date"], "defaults": {"date": _TODAY_YMD}},
    "stock_gpzy_pledge_ratio_em": {"label": "股权质押比例", "desc": "全市场股权质押比例（date=交易日）", "cat": "市场资金", "params": ["date"], "defaults": {"date": _TODAY_YMD}},

    # ---------- 财务报表 ----------
    "stock_zcfz_em": {"label": "资产负债表（全市场）", "desc": "全市场资产负债表（date=报告期，默认最近已披露季度）", "cat": "财务报表", "params": ["date"], "defaults": {"date": _REPORT_PERIOD}},
    "stock_lrb_em": {"label": "利润表（全市场）", "desc": "全市场利润表（date=报告期，默认最近已披露季度）", "cat": "财务报表", "params": ["date"], "defaults": {"date": _REPORT_PERIOD}},
    "stock_xjll_em": {"label": "现金流量表（全市场）", "desc": "全市场现金流量表（date=报告期，默认最近已披露季度）", "cat": "财务报表", "params": ["date"], "defaults": {"date": _REPORT_PERIOD}},
    "stock_yjbb_em": {"label": "业绩报表（全市场）", "desc": "业绩报表（date=报告期，默认最近已披露季度）", "cat": "财务报表", "params": ["date"], "defaults": {"date": _REPORT_PERIOD}},
    "stock_financial_analysis_indicator": {"label": "个股财务指标", "desc": "个股财务分析指标（代码, start_year=起始年份）", "cat": "财务报表", "params": ["symbol", "start_year"], "defaults": {"start_year": ""}},

    # ---------- 宏观中国 ----------
    "macro_china_gdp": {"label": "GDP（国内生产总值）", "desc": "中国季度GDP：总量、同比、三次产业", "cat": "宏观中国"},
    "macro_china_gdp_yearly": {"label": "GDP年率", "desc": "中国GDP同比增速序列", "cat": "宏观中国"},
    "macro_china_cpi": {"label": "CPI（居民消费价格指数）", "desc": "全国居民消费价格指数月度序列", "cat": "宏观中国"},
    "macro_china_cpi_monthly": {"label": "CPI月率", "desc": "CPI环比月度序列", "cat": "宏观中国"},
    "macro_china_cpi_yearly": {"label": "CPI年率", "desc": "CPI同比月度序列", "cat": "宏观中国"},
    "macro_china_ppi": {"label": "PPI（工业生产者出厂价格）", "desc": "工业生产者出厂价格指数", "cat": "宏观中国"},
    "macro_china_ppi_yearly": {"label": "PPI年率", "desc": "PPI同比序列", "cat": "宏观中国"},
    "macro_china_pmi": {"label": "PMI（采购经理指数）", "desc": "制造业/非制造业PMI月度序列", "cat": "宏观中国"},
    "macro_china_pmi_yearly": {"label": "PMI年率", "desc": "PMI同比序列", "cat": "宏观中国"},
    "macro_china_money_supply": {"label": "货币供应量 M0/M1/M2", "desc": "货币供应量月度数据（含同比）", "cat": "宏观中国"},
    "macro_china_lpr": {"label": "LPR（贷款市场报价利率）", "desc": "1年期/5年期以上LPR历史", "cat": "宏观中国"},
    "macro_china_shrzgm": {"label": "社会融资规模增量", "desc": "社会融资规模增量月度数据", "cat": "宏观中国"},
    "macro_cnbs": {"label": "宏观杠杆率", "desc": "居民/非金融企业/政府部门杠杆率", "cat": "宏观中国"},
    "macro_china_fx_gold": {"label": "外汇与黄金储备", "desc": "官方外汇储备、黄金储备月度数据", "cat": "宏观中国"},
    "macro_china_trade_balance": {"label": "贸易差额", "desc": "进出口贸易差额月度数据", "cat": "宏观中国"},
    "macro_china_exports_yoy": {"label": "出口年率", "desc": "出口金额同比增速", "cat": "宏观中国"},
    "macro_china_consumer_goods_retail": {"label": "社会消费品零售总额", "desc": "社会消费品零售总额及同比", "cat": "宏观中国"},
    "macro_china_urban_unemployment": {"label": "城镇调查失业率", "desc": "全国城镇调查失业率月度数据", "cat": "宏观中国"},
    "macro_china_industrial_production_yoy": {"label": "工业增加值同比", "desc": "规模以上工业增加值同比增速", "cat": "宏观中国"},

    # ---------- 宏观美国 ----------
    "macro_usa_gdp_monthly": {"label": "美国GDP", "desc": "美国季度GDP数据", "cat": "宏观美国"},
    "macro_usa_cpi_monthly": {"label": "美国CPI月率", "desc": "美国CPI环比月度序列", "cat": "宏观美国"},
    "macro_usa_non_farm": {"label": "美国非农就业", "desc": "美国非农就业人口变动", "cat": "宏观美国"},
    "macro_usa_unemployment_rate": {"label": "美国失业率", "desc": "美国失业率月度序列", "cat": "宏观美国"},

    # ---------- 其他 ----------
    "stock_xgsr_ths": {"label": "新股申购与上市", "desc": "新股申购/上市信息（同花顺）", "cat": "其他"},
    "energy_oil_hist": {"label": "成品油调价历史", "desc": "国内成品油价格调整历史", "cat": "其他"},
    "article_epu_index": {"label": "经济政策不确定性指数", "desc": "EPU指数；symbol 取 China/USA/Japan 等", "cat": "其他", "params": ["symbol"], "defaults": {"symbol": "China"}},
}

TS_DATA_FUNCS = {
    # ---------- 行情 ----------
    "daily": {"label": "A股日K", "desc": "A股日线 (ts_code, start, end)", "cat": "行情", "params": ["ts_code", "start_date", "end_date"]},
    "weekly": {"label": "A股周K", "desc": "A股周线 (ts_code, start, end)", "cat": "行情", "params": ["ts_code", "start_date", "end_date"]},
    "monthly": {"label": "A股月K", "desc": "A股月线 (ts_code, start, end)", "cat": "行情", "params": ["ts_code", "start_date", "end_date"]},
    "index_daily": {"label": "指数K线", "desc": "指数日线 (ts_code, start, end)", "cat": "行情", "params": ["ts_code", "start_date", "end_date"]},
    "futures_daily": {"label": "期货日线", "desc": "期货日线 (ts_code, start, end)", "cat": "行情", "params": ["ts_code", "start_date", "end_date"]},
    "daily_basic": {"label": "每日指标（PE/PB/换手）", "desc": "每日估值与股本指标 (ts_code, start, end)", "cat": "行情", "params": ["ts_code", "start_date", "end_date"]},

    # ---------- 基础信息 ----------
    "stock_basic": {"label": "A股列表", "desc": "A股基础信息列表", "cat": "基础信息"},
    "index_basic": {"label": "指数基本信息", "desc": "指数基础信息 (ts_code)", "cat": "基础信息", "params": ["ts_code"]},
    "trade_cal": {"label": "交易日历", "desc": "交易日历 (start, end)", "cat": "基础信息", "params": ["start_date", "end_date"]},
    "stock_company": {"label": "上市公司基本信息", "desc": "上市公司基本信息 (ts_code)", "cat": "基础信息", "params": ["ts_code"]},
    "index_weight": {"label": "指数成分与权重", "desc": "指数成分股权重 (ts_code, start, end)", "cat": "基础信息", "params": ["ts_code", "start_date", "end_date"]},

    # ---------- 财务 ----------
    "income": {"label": "利润表", "desc": "利润表 (ts_code, start, end)", "cat": "财务", "params": ["ts_code", "start_date", "end_date"]},
    "balancesheet": {"label": "资产负债表", "desc": "资产负债表 (ts_code, start, end)", "cat": "财务", "params": ["ts_code", "start_date", "end_date"]},
    "cashflow": {"label": "现金流量表", "desc": "现金流量表 (ts_code, start, end)", "cat": "财务", "params": ["ts_code", "start_date", "end_date"]},
    "fina_indicator": {"label": "财务指标", "desc": "财务分析指标 (ts_code, start, end)", "cat": "财务", "params": ["ts_code", "start_date", "end_date"]},
    "forecast": {"label": "业绩预告", "desc": "业绩预告 (ts_code, start, end)", "cat": "财务", "params": ["ts_code", "start_date", "end_date"]},
    "express": {"label": "业绩快报", "desc": "业绩快报 (ts_code, start, end)", "cat": "财务", "params": ["ts_code", "start_date", "end_date"]},
    "dividend": {"label": "分红送股", "desc": "分红送股 (ts_code)", "cat": "财务", "params": ["ts_code"]},

    # ---------- 资金与股东 ----------
    "moneyflow": {"label": "个股资金流向", "desc": "个股资金流向 (ts_code, start, end)", "cat": "资金股东", "params": ["ts_code", "start_date", "end_date"]},
    "moneyflow_hsgt": {"label": "沪深港通资金流", "desc": "沪深港通资金流向 (start, end)", "cat": "资金股东", "params": ["start_date", "end_date"]},
    "ggt_daily": {"label": "港股通每日成交", "desc": "沪深港通资金流水 (trade_date)", "cat": "资金股东", "params": ["trade_date"]},
    "hk_hold": {"label": "港股持股", "desc": "港股持股记录 (ts_code, start, end)", "cat": "资金股东", "params": ["ts_code", "start_date", "end_date"]},
    "top10_holders": {"label": "十大股东", "desc": "十大流通股东 (ts_code, start, end)", "cat": "资金股东", "params": ["ts_code", "start_date", "end_date"]},
    "top_list": {"label": "龙虎榜每日明细", "desc": "龙虎榜每日明细 (trade_date)", "cat": "资金股东", "params": ["trade_date"]},
    "margin_detail": {"label": "融资融券明细", "desc": "融资融券明细 (ts_code, start, end)", "cat": "资金股东", "params": ["ts_code", "start_date", "end_date"]},
    "repurchase": {"label": "股票回购", "desc": "股票回购 (start, end)", "cat": "资金股东", "params": ["start_date", "end_date"]},

    # ---------- 基金 ----------
    "fund_nav": {"label": "基金净值", "desc": "基金净值 (ts_code, start, end)", "cat": "基金", "params": ["ts_code", "start_date", "end_date"]},
    "fund_basic": {"label": "基金列表", "desc": "基金基础信息列表", "cat": "基金"},
    "fund_share": {"label": "基金份额", "desc": "基金份额 (ts_code, start, end)", "cat": "基金", "params": ["ts_code", "start_date", "end_date"]},
}


# ==================== 路由异常响应统一映射 ====================
def exc_response(e, default_code: int = 500):
    """把路由 except 捕获的异常映射为合适的 HTTP 响应。

    背景：大量路由用 `try/except Exception -> jsonify(...), 500`，
    导致「请求缺参数」这类客户端错误也返回 500，前端只能看到"服务器内部错误"。
    本函数把入参类异常降级为 400，其余保持 500。

    - KeyError            → 400，提示缺少哪个字段
    - TypeError(NoneType) → 400，缺少文件路径/必需参数
    - ValueError          → 400，参数值不合法
    - FileNotFoundError   → 404
    - 其它                → 500（消息透传，并记完整堆栈便于定位）
    """
    if isinstance(e, KeyError):
        key = e.args[0] if e.args else 'unknown'
        return jsonify({"ok": False, "error": f"缺少必需参数：{key}", "code": "BAD_REQUEST"}), 400
    msg = str(e) or type(e).__name__
    if isinstance(e, TypeError) and 'NoneType' in msg:
        return jsonify({"ok": False, "error": "缺少文件路径或必需参数", "code": "BAD_REQUEST"}), 400
    if isinstance(e, FileNotFoundError):
        fn = getattr(e, 'filename', None) or msg
        return jsonify({"ok": False, "error": f"文件不存在：{fn}", "code": "NOT_FOUND"}), 404
    if isinstance(e, (ValueError,)) and len(msg) < 300:
        return jsonify({"ok": False, "error": msg, "code": "BAD_REQUEST"}), 400
    # 归为服务端错误：必须落日志，否则 500 现场无法复盘（此前 exc_response 静默吞异常，
    # 导致日志里只剩一行 "- 500"，拿不到堆栈）。
    try:
        logger.error(
            f"[exc_response] {type(e).__name__}: {msg} | request={request.method} {request.path}",
            exc_info=True,
        )
    except Exception:
        pass
    return jsonify({"error": msg, "code": "INTERNAL_ERROR"}), default_code
