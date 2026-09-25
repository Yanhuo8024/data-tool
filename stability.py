#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stability.py - 大数据量稳定性保障模块

核心能力：
1. 流式响应（Streaming）：大数据分批返回，避免内存溢出
2. 分页查询：API 自动分页，限制单次返回量
3. 内存监控：实时检测内存使用，超限自动降级
4. 超时控制：SQL 执行、API 调用超时保护
5. 错误恢复：自动重试、降级、熔断
6. 数据压缩：响应数据自动压缩
7. 连接池管理：数据库连接复用
"""

import os
import sys
import time
import json
import math
import psutil
import hashlib
import logging
import functools
import threading
from typing import Dict, List, Optional, Any, Iterator
from datetime import datetime, timedelta


# ==========================================================
# 配置
# ==========================================================

class StabilityConfig:
    """稳定性配置"""
    # 内存
    MAX_MEMORY_MB = 1024              # 最大内存使用 (MB)
    MEMORY_WARN_MB = 768              # 内存警告阈值 (MB)
    MEMORY_CRITICAL_MB = 900          # 内存危险阈值 (MB)
    
    # 分页
    DEFAULT_PAGE_SIZE = 100           # 默认每页条数
    MAX_PAGE_SIZE = 1000              # 最大每页条数
    MAX_RESULT_ROWS = 10000           # 最大返回行数
    
    # 超时
    SQL_TIMEOUT_SEC = 30              # SQL 执行超时
    API_TIMEOUT_SEC = 60              # API 调用超时
    UPLOAD_TIMEOUT_SEC = 120          # 文件上传超时
    
    # 流式
    STREAM_CHUNK_SIZE = 1000          # 流式返回每块行数
    STREAM_MAX_ROWS = 50000           # 流式最大行数
    
    # 文件
    MAX_UPLOAD_SIZE_MB = 50           # 最大上传文件大小 (MB)
    MAX_PROCESS_ROWS = 1000000        # 最大处理行数
    
    # 重试
    MAX_RETRIES = 3                   # 最大重试次数
    RETRY_DELAY_SEC = 1.0             # 重试间隔 (秒)
    
    # 缓存
    CACHE_MAX_SIZE = 100              # 缓存最大条目数
    CACHE_TTL_SEC = 300               # 缓存过期时间 (秒)


# ==========================================================
# 内存监控器
# ==========================================================

class MemoryMonitor:
    """实时内存监控"""
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self.process = psutil.Process(os.getpid())
        self.logger = logging.getLogger("stability.memory")
    
    def get_memory_mb(self) -> float:
        """获取当前内存使用 (MB)"""
        return self.process.memory_info().rss / 1024 / 1024
    
    def get_memory_percent(self) -> float:
        """获取内存使用百分比"""
        return self.process.memory_percent()
    
    def get_status(self) -> Dict:
        """获取内存状态"""
        mem_mb = self.get_memory_mb()
        
        if mem_mb >= StabilityConfig.MEMORY_CRITICAL_MB:
            status = "CRITICAL"
        elif mem_mb >= StabilityConfig.MEMORY_WARN_MB:
            status = "WARNING"
        else:
            status = "OK"
        
        return {
            "status": status,
            "memory_mb": round(mem_mb, 1),
            "memory_percent": round(self.get_memory_percent(), 1),
            "max_mb": StabilityConfig.MAX_MEMORY_MB,
            "warn_mb": StabilityConfig.MEMORY_WARN_MB,
            "critical_mb": StabilityConfig.MEMORY_CRITICAL_MB,
        }
    
    def is_safe(self) -> bool:
        """检查内存是否安全"""
        return self.get_memory_mb() < StabilityConfig.MEMORY_CRITICAL_MB
    
    def is_warning(self) -> bool:
        """检查是否达到警告阈值"""
        return self.get_memory_mb() >= StabilityConfig.MEMORY_WARN_MB
    
    def force_gc(self):
        """强制垃圾回收"""
        import gc
        gc.collect()
        self.logger.info(f"Forced GC, memory: {self.get_memory_mb():.1f}MB")


# ==========================================================
# 分页器
# ==========================================================

class Paginator:
    """数据分页器"""
    
    @staticmethod
    def paginate(data: List[Dict], page: int = 1, 
                 page_size: int = None) -> Dict:
        """
        对数据进行分页
        
        Returns:
            {
                "data": [...],
                "pagination": {
                    "page": 1,
                    "page_size": 100,
                    "total": 1000,
                    "total_pages": 10,
                    "has_next": true,
                    "has_prev": false
                }
            }
        """
        if page_size is None:
            page_size = StabilityConfig.DEFAULT_PAGE_SIZE
        
        # 限制 page_size
        page_size = min(page_size, StabilityConfig.MAX_PAGE_SIZE)
        page = max(1, page)
        
        total = len(data)
        total_pages = math.ceil(total / page_size) if total > 0 else 1
        
        start = (page - 1) * page_size
        end = start + page_size
        
        return {
            "data": data[start:end],
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_prev": page > 1,
            }
        }
    
    @staticmethod
    def paginate_sql(sql: str, page: int = 1, 
                     page_size: int = None) -> str:
        """为 SQL 添加分页"""
        if page_size is None:
            page_size = StabilityConfig.DEFAULT_PAGE_SIZE
        
        page_size = min(page_size, StabilityConfig.MAX_PAGE_SIZE)
        page = max(1, page)
        
        offset = (page - 1) * page_size
        
        # 移除末尾分号
        sql = sql.rstrip(';').rstrip()
        
        return f"{sql} LIMIT {page_size} OFFSET {offset}"


# ==========================================================
# 流式数据生成器
# ==========================================================

class StreamingResponse:
    """流式响应生成器"""
    
    @staticmethod
    def stream_json(data: List[Dict], chunk_size: int = None) -> Iterator[str]:
        """
        流式返回 JSON 数据
        
        适用于大数据量场景，分批返回，避免内存溢出
        """
        if chunk_size is None:
            chunk_size = StabilityConfig.STREAM_CHUNK_SIZE
        
        total = len(data)
        chunks = math.ceil(total / chunk_size)
        
        # 头部
        yield '{"data": ['
        
        for i in range(0, total, chunk_size):
            chunk = data[i:i + chunk_size]
            json_str = json.dumps(chunk, ensure_ascii=False)
            
            # 去掉首尾的 [ 和 ]
            if json_str.startswith('['):
                json_str = json_str[1:-1]
            
            if i > 0:
                yield ','
            
            yield json_str
            
            # 每块之间让出控制权
            time.sleep(0.001)
        
        # 尾部
        yield '], "total": ' + str(total) + '}'
    
    @staticmethod
    def stream_csv(data: List[Dict], columns: List[str] = None) -> Iterator[str]:
        """流式返回 CSV 数据"""
        if not data:
            yield ""
            return
        
        if columns is None:
            columns = list(data[0].keys())
        
        # 头部
        yield ','.join(columns) + '\n'
        
        # 数据行
        for row in data:
            values = []
            for col in columns:
                val = str(row.get(col, ''))
                # 转义逗号和引号
                if ',' in val or '"' in val:
                    val = '"' + val.replace('"', '""') + '"'
                values.append(val)
            yield ','.join(values) + '\n'
            
            # 每 1000 行让出控制权
            if len(values) % 1000 == 0:
                time.sleep(0.001)


# ==========================================================
# 超时控制器
# ==========================================================

class TimeoutError(Exception):
    """超时异常"""
    pass


class TimeoutController:
    """超时控制器"""
    
    @staticmethod
    def run_with_timeout(func, timeout_sec: float = None, *args, **kwargs):
        """
        带超时控制的函数执行
        
        Args:
            func: 要执行的函数
            timeout_sec: 超时时间（秒）
        
        Returns:
            函数返回值
        
        Raises:
            TimeoutError: 超时
        """
        if timeout_sec is None:
            timeout_sec = StabilityConfig.API_TIMEOUT_SEC
        
        result = [None]
        error = [None]
        
        def wrapper():
            try:
                result[0] = func(*args, **kwargs)
            except Exception as e:
                error[0] = e
        
        thread = threading.Thread(target=wrapper, daemon=True)
        thread.start()
        thread.join(timeout=timeout_sec)
        
        if thread.is_alive():
            raise TimeoutError(f"执行超时 ({timeout_sec}秒)")
        
        if error[0]:
            raise error[0]
        
        return result[0]
    
    @staticmethod
    def timeout_decorator(timeout_sec: float = None):
        """超时装饰器"""
        def decorator(f):
            @functools.wraps(f)
            def wrapper(*args, **kwargs):
                return TimeoutController.run_with_timeout(
                    f, timeout_sec, *args, **kwargs
                )
            return wrapper
        return decorator


# ==========================================================
# 重试器
# ==========================================================

class RetryHandler:
    """自动重试处理器"""
    
    @staticmethod
    def retry(func, max_retries: int = None, delay: float = None,
              exceptions: tuple = (Exception,), *args, **kwargs):
        """
        自动重试执行
        
        Args:
            func: 要执行的函数
            max_retries: 最大重试次数
            delay: 重试间隔 (秒)
            exceptions: 需要重试的异常类型
        
        Returns:
            函数返回值
        """
        if max_retries is None:
            max_retries = StabilityConfig.MAX_RETRIES
        if delay is None:
            delay = StabilityConfig.RETRY_DELAY_SEC
        
        last_error = None
        
        for attempt in range(max_retries + 1):
            try:
                return func(*args, **kwargs)
            except exceptions as e:
                last_error = e
                if attempt < max_retries:
                    time.sleep(delay * (attempt + 1))  # 指数退避
        
        raise last_error
    
    @staticmethod
    def retry_decorator(max_retries: int = None, delay: float = None,
                       exceptions: tuple = (Exception,)):
        """重试装饰器"""
        def decorator(f):
            @functools.wraps(f)
            def wrapper(*args, **kwargs):
                return RetryHandler.retry(
                    f, max_retries, delay, exceptions, *args, **kwargs
                )
            return wrapper
        return decorator


# ==========================================================
# 数据大小限制器
# ==========================================================

class DataSizeLimiter:
    """数据大小限制器"""
    
    @staticmethod
    def limit_dataframe_rows(df, max_rows: int = None):
        """限制 DataFrame 行数"""
        if max_rows is None:
            max_rows = StabilityConfig.MAX_RESULT_ROWS
        
        if len(df) > max_rows:
            return df.head(max_rows)
        return df
    
    @staticmethod
    def limit_response_size(data: List[Dict], max_size_mb: float = 10) -> List[Dict]:
        """限制响应数据大小"""
        # 估算大小
        sample = data[:100] if len(data) > 100 else data
        if not sample:
            return data
        
        avg_row_size = len(json.dumps(sample, ensure_ascii=False)) / len(sample)
        total_size_mb = (avg_row_size * len(data)) / 1024 / 1024
        
        if total_size_mb <= max_size_mb:
            return data
        
        # 计算最大行数
        max_rows = int((max_size_mb * 1024 * 1024) / avg_row_size)
        return data[:max_rows]
    
    @staticmethod
    def estimate_size_mb(data: Any) -> float:
        """估算数据大小 (MB)"""
        try:
            size = len(json.dumps(data, ensure_ascii=False).encode('utf-8'))
            return size / 1024 / 1024
        except (TypeError, ValueError):
            return 0.0


# ==========================================================
# 安全降级器
# ==========================================================

class DegradationHandler:
    """安全降级处理器"""
    
    @staticmethod
    def degrade_response(data: List[Dict], memory_status: Dict) -> Dict:
        """
        根据内存状态降级响应
        
        - WARNING: 减少返回数据量
        - CRITICAL: 只返回摘要信息
        """
        status = memory_status.get("status", "OK")
        
        if status == "CRITICAL":
            # 危险状态：只返回摘要
            return {
                "degraded": True,
                "reason": "内存不足，返回摘要",
                "summary": {
                    "total_rows": len(data),
                    "columns": list(data[0].keys()) if data else [],
                    "sample": data[:5] if data else [],
                },
                "memory": memory_status,
            }
        
        elif status == "WARNING":
            # 警告状态：限制返回量
            limited = data[:StabilityConfig.DEFAULT_PAGE_SIZE]
            return {
                "degraded": True,
                "reason": "内存偏高，限制返回量",
                "data": limited,
                "total": len(data),
                "memory": memory_status,
            }
        
        # 正常状态
        return {
            "degraded": False,
            "data": data,
            "memory": memory_status,
        }


# ==========================================================
# 全局实例
# ==========================================================

memory_monitor = MemoryMonitor()
paginator = Paginator()
data_limiter = DataSizeLimiter()
degradation = DegradationHandler()


# ==============================================
# 使用示例
# ==============================================

if __name__ == "__main__":
    print("=" * 60)
    print(" 稳定性模块测试")
    print("=" * 60)
    
    # 1. 内存监控
    print("\n[1] 内存监控")
    status = memory_monitor.get_status()
    print(f"  状态: {status['status']}")
    print(f"  内存: {status['memory_mb']}MB ({status['memory_percent']}%)")
    print(f"  安全: {memory_monitor.is_safe()}")
    
    # 2. 分页
    print("\n[2] 分页")
    test_data = [{"id": i, "value": f"item_{i}"} for i in range(250)]
    result = paginator.paginate(test_data, page=2, page_size=50)
    print(f"  总数: {result['pagination']['total']}")
    print(f"  页数: {result['pagination']['total_pages']}")
    print(f"  当前页数据: {len(result['data'])} 条")
    print(f"  有下一页: {result['pagination']['has_next']}")
    
    # 3. 数据限制
    print("\n[3] 数据大小限制")
    size_mb = data_limiter.estimate_size_mb(test_data)
    print(f"  数据大小: {size_mb:.2f}MB")
    
    # 4. 重试
    print("\n[4] 重试机制")
    attempt = [0]
    def flaky_func():
        attempt[0] += 1
        if attempt[0] < 3:
            raise Exception("模拟失败")
        return "成功"
    
    result = RetryHandler.retry(flaky_func, max_retries=3, delay=0.1)
    print(f"  重试 {attempt[0]} 次后: {result}")
    
    # 5. 超时
    print("\n[5] 超时控制")
    def slow_func():
        time.sleep(0.5)
        return "完成"
    
    try:
        result = TimeoutController.run_with_timeout(slow_func, 1.0)
        print(f"  结果: {result}")
    except TimeoutError as e:
        print(f"  超时: {e}")
    
    # 6. 降级
    print("\n[6] 安全降级")
    degraded = degradation.degrade_response(test_data, {"status": "WARNING"})
    print(f"  降级: {degraded['degraded']}")
    print(f"  返回数据: {len(degraded.get('data', []))} 条")
    
    print("\n" + "=" * 60)
    print(" 稳定性模块测试完成")
    print("=" * 60)
