"""运行时补丁回归：py_mini_racer 并发创建 FATAL 修复（2026-09-18 真实崩溃）。

故障现场：20:45 三阶段并行管线运行期间，服务进程以
`[FATAL:partition_address_space.cc(243)] Check failed: !IsConfigurablePoolInitialized()`
整体退出（栈顶 py_mini_racer，akshare 依赖的 V8）。复现脚本见
`.workbuddy/_probe_miniracer_threads.py`（并发 3 必崩）。
"""
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _app_python():
    """优先用应用便携解释器（py_mini_racer 只装在它里面）。"""
    p = os.path.join(ROOT, 'python-portable', 'python.exe')
    return p if os.path.exists(p) else sys.executable


def test_mini_racer_creation_lock_installed():
    """补丁必须把 MiniRacer 换成带锁工厂（否则并发创建会打死进程）。"""
    from runtime_patches import apply_mini_racer_patch
    apply_mini_racer_patch()
    try:
        import py_mini_racer
    except Exception:
        pytest.skip('当前解释器未安装 py_mini_racer')
    assert getattr(py_mini_racer.MiniRacer, '_dt_locked', False), \
        'MiniRacer 创建锁未安装：并发创建将 FATAL 杀死整个服务进程'


def test_concurrent_mini_racer_creation_does_not_crash():
    """3 线程并发创建 MiniRacer 必须存活。

    ⚠️ 用**子进程**执行：若补丁被回退，故障是进程级 FATAL（Python 无法捕获），
    在同一个 pytest 进程里跑会把整个测试会话一起带走、看不到失败原因。
    """
    code = '\n'.join([
        'import sys, threading',
        'sys.path.insert(0, %r)' % ROOT,
        'from runtime_patches import apply_all',
        'apply_all()',
        'import py_mini_racer',
        'res, lock = [], threading.Lock()',
        'barrier = threading.Barrier(3)',
        'def w():',
        '    barrier.wait(timeout=20)',
        '    v = py_mini_racer.MiniRacer().eval("6*7")',
        '    with lock: res.append(v)',
        'ts = [threading.Thread(target=w) for _ in range(3)]',
        '[t.start() for t in ts]',
        '[t.join() for t in ts]',
        'print("OK", len(res), sorted(res))',
        'assert sorted(res) == [42, 42, 42], res',
    ])
    proc = subprocess.run([_app_python(), '-X', 'utf8', '-c', code],
                          capture_output=True, text=True, timeout=180, cwd=ROOT)
    detail = ((proc.stderr or '') + (proc.stdout or ''))[-600:]
    assert proc.returncode == 0, f'并发创建 MiniRacer 时进程崩溃（补丁失效？）: {detail}'
    assert 'OK 3 [42, 42, 42]' in (proc.stdout or ''), f'并发结果异常: {detail}'
