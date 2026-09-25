"""运行时补丁：修复第三方库在本机的致命缺陷（进程级崩溃）。

### 1) py_mini_racer 并发创建 V8 实例会 FATAL 杀掉整个进程

**现象**（Windows + 便携 Python 3.11）：
多线程**同时**执行 `py_mini_racer.MiniRacer()` → 进程立即以
`[FATAL:partition_address_space.cc(243)] Check failed: !IsConfigurablePoolInitialized()`
退出，Python 层无法捕获，Flask/waitress 整站随之下线。

**影响面**：akshare 若干接口内部会创建 MiniRacer（如 `bond_zh_cov`、`bond_zh_sina`、
`bond_issue_cninfo`、`air_zhenqi`）。多 Agent 管线**三阶段并行**后，任意两个阶段
同时命中此类接口即可打死服务（2026-09-18 20:45 实测发生一次）。

**取证**（均可复现）：
- `.workbuddy/_probe_miniracer_threads.py`：并发 1 正常、并发 3/5 **100% FATAL**。
- `.workbuddy/_probe_miniracer_fix.py`：`prewarm`（主线程预热）与 `lock`（创建加锁）
  两种修法各跑 3 次全部成功。

**修法**（本模块）：把 `MiniRacer` 的**创建**串行化。akshare 的调用形如
`import py_mini_racer` + `py_mini_racer.MiniRacer()`（运行时属性查找），因此替换包属性
即可覆盖全部调用点，无需改第三方代码。返回的仍是原生 MiniRacer 实例（类型不变），
只是创建过程互斥；创建完成后各线程独立使用各自实例，不引入串行化开销。

环境变量 `DT_MINI_RACER_PATCH=0` 可关闭（回退到未修复行为，仅用于对比取证）。
"""
import os
import threading

_LOCK = threading.Lock()
_applied = False


def apply_mini_racer_patch() -> bool:
    """幂等安装 MiniRacer 创建锁。返回是否已处于「已修复」状态。

    任何异常都不得向上抛——这是启动期补丁，绝不能因为第三方库缺失/版本差异而拖垮应用。
    """
    global _applied
    if _applied:
        return True
    if os.environ.get('DT_MINI_RACER_PATCH', '1') in ('0', '', 'false', 'False'):
        _applied = True
        return False
    try:
        import py_mini_racer
    except Exception:
        _applied = True       # 未安装该库（如单元测试环境）→ 无需补丁
        return False

    orig = getattr(py_mini_racer, 'MiniRacer', None)
    if orig is None or getattr(orig, '_dt_locked', False):
        _applied = True
        return False

    def _locked_factory(*args, **kwargs):
        # 只需保护「创建」：V8 configurable pool 的初始化不可重入。
        with _LOCK:
            return orig(*args, **kwargs)

    _locked_factory._dt_locked = True
    _locked_factory._dt_orig = orig
    _locked_factory.__name__ = 'MiniRacer'
    py_mini_racer.MiniRacer = _locked_factory
    _applied = True
    return True


def apply_all() -> None:
    """应用全部运行时补丁（启动期调用一次即可，幂等）。"""
    apply_mini_racer_patch()
