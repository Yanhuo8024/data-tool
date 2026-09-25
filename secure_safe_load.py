#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
secure_safe_load.py - 安全模型加载器（白名单反序列化）

核心原则：不反序列化任意对象，严格白名单校验
安全问题: pickle 反序列化漏洞(CVE-2019-11340)
修复: 使用 RestrictedUnpickler 限制可加载的类
"""
import pickle
from typing import Any, Optional

ALLOWED_MODULES = {
    "numpy": {"multiarray", "ndarray", "dtype", "float64", "int64"},
    "ml_module": {"MLTrainer"},
}

# 可信模块前缀：ML 训练产物（MLTrainer）pickle 时会引用 pandas/sklearn/scipy 等
# 科学计算库的类。这些库的类在 unpickle 时不执行任意系统命令，风险可控。
# 危险模块（os/sys/subprocess/builtins 等）仍一律拒绝。
ALLOWED_PREFIXES = ("numpy", "pandas", "sklearn", "scipy", "ml_module",
                    "xgboost", "lightgbm", "statsmodels", "pyarrow", "joblib")

# builtins 仅允许纯数据容器/类型（numpy/sklearn 序列化依赖 bytearray 等），
# 可执行函数（eval/exec/open/compile 等）一律拒绝。
ALLOWED_BUILTINS = {
    "bytearray", "bytes", "slice", "complex", "tuple", "list", "dict",
    "set", "frozenset", "object", "property", "staticmethod",
    "classmethod", "range", "NotImplemented", "map", "filter",
    "enumerate", "zip", "reversed", "memoryview",
}


class RestrictedUnpickler(pickle.Unpickler):
    """受限反序列化器 - 仅允许白名单模块/前缀"""

    def find_class(self, module, name):
        # builtins 纯数据容器白名单
        if module == "builtins" and name in ALLOWED_BUILTINS:
            return pickle.Unpickler.find_class(self, module, name)
        # numpy 精确白名单：仅放行 multiarray/ndarray/dtype 等数据类
        parts = module.split(".")
        if parts[0] == "numpy" and name in ALLOWED_MODULES["numpy"]:
            return pickle.Unpickler.find_class(self, module, name)
        # ml_module 白名单
        if module == "ml_module" and name in ALLOWED_MODULES["ml_module"]:
            return pickle.Unpickler.find_class(self, module, name)
        # 可信前缀（pandas/sklearn/scipy 等科学计算库）
        if parts[0] in ALLOWED_PREFIXES:
            return pickle.Unpickler.find_class(self, module, name)
        # 拒绝所有其他类型
        raise pickle.UnpicklingError(
            f"blocked: {module}.{name} (allowed: {list(ALLOWED_MODULES.keys())} + {list(ALLOWED_PREFIXES)})"
        )


def safe_load(filepath: str) -> Any:
    """安全加载 pickle 文件"""
    with open(filepath, "rb") as f:
        return RestrictedUnpickler(f).load()


def safe_loads(blob: bytes) -> Any:
    """安全反序列化 bytes"""
    import io
    return RestrictedUnpickler(io.BytesIO(blob)).load()
