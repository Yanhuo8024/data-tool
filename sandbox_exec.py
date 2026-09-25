#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sandbox_exec.py - 安全的代码执行沙箱

保持兼容的原有函数：
- validate_upload_file: 验证上传文件安全性
- safe_formula_eval: 安全计算公式
- SandboxError: 沙箱错误基类

新增函数：
- execute_code: 执行 Python/JS 代码（入口）
- execute_python: 安全执行 Python（子进程隔离）
- execute_javascript: 安全执行 JavaScript（Node.js）
"""

import sys
import io
import os
import json
import base64
import traceback
import tempfile
import subprocess
from pathlib import Path
from typing import Dict, Any, Optional

# ==================== 配置 ====================

MAX_MEMORY_MB = 512
MAX_EXECUTION_TIME = 30

# 静态审查策略统一收敛到 plugin_sandbox（AST 白名单+黑名单），
# 字符串黑名单 FORBIDDEN_PATTERNS 已移除（可被 __import__/exec 拼接绕过）。

# ==================== 兼容原有接口 ====================

class SandboxError(Exception):
    """沙箱错误基类"""
    pass


def validate_upload_file(filename: str, content: bytes) -> tuple:
    """验证上传文件安全性，返回 (is_safe, error_msg)"""
    if not filename:
        return False, "文件名为空"
    if len(filename) > 255:
        return False, "文件名过长"
    allowed_ext = {'.csv', '.xlsx', '.xls', '.parquet', '.json', '.txt'}
    ext = Path(filename).suffix.lower()
    if ext not in allowed_ext:
        return False, f"不支持的文件格式: {ext}"
    max_size = 100 * 1024 * 1024
    if len(content) > max_size:
        return False, f"文件过大: {len(content)/1024/1024:.1f}MB"
    return True, ""


def safe_formula_eval(df, formula: str, new_col: str):
    """安全计算公式，返回新增该列后的 DataFrame"""
    forbidden_kw = ['import', 'exec', 'eval', 'compile', '__import__', 'open', 'file']
    for f in forbidden_kw:
        if f in formula.lower():
            raise SandboxError(f"公式中禁止使用: {f}")
    try:
        result = df.eval(formula, engine='python')
    except Exception as e:
        raise SandboxError(f"公式计算错误: {str(e)}")
    # df.eval 返回 Series；赋回新列后返回完整 DataFrame
    out = df.copy()
    out[new_col] = result
    return out


# ==================== 代码执行沙箱 ====================

def check_code_safety(code: str) -> Optional[str]:
    """检查代码安全性，返回错误信息或 None。

    使用 plugin_sandbox 的 AST 静态审查（import 白名单 + 危险函数黑名单 +
    dunder 逃逸拦截），避免字符串黑名单被 ``__import__``/exec 拼接等手法绕过。
    语法错误不在此拦截——交给子进程执行时给出带行号的原始报错。
    """
    import ast

    import plugin_sandbox
    try:
        ast.parse(code)
    except SyntaxError:
        return None  # 语法错误放行，由子进程报错
    try:
        plugin_sandbox.audit_plugin_source(code)
    except plugin_sandbox.SandboxViolation as e:
        return str(e)
    return None


def execute_python(code: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
    """安全执行 Python 代码（子进程隔离 + 内存限额 + context 变量注入）"""
    safety_error = check_code_safety(code)
    if safety_error:
        return {"ok": False, "error": safety_error, "stdout": "", "stderr": ""}

    # P1-8：context 变量注入。调用方传入的 JSON 可序列化数据以变量形式提供给代码；
    # 不可序列化的对象（如 DataFrame 直传）会被丢弃并在 stderr 提示。
    injected = {}
    ctx_note = ""
    if context and isinstance(context, dict):
        for k, v in context.items():
            if not str(k).isidentifier():
                continue
            try:
                json.dumps(v)
                injected[str(k)] = v
            except (TypeError, ValueError):
                ctx_note += f"- {k}: 非 JSON 可序列化，已跳过\n"
    ctx_json = json.dumps(injected, ensure_ascii=False, default=str)
    # base64 载体：防止内容中的引号/三引号破坏包装代码
    ctx_b64 = base64.b64encode(ctx_json.encode('utf-8')).decode('ascii')

    # 包装代码：捕获输出 + 自动检测 matplotlib + 注入 context 变量
    wrapped_code = f'''
import sys, io, json, traceback, base64
_old_stdout = sys.stdout
_old_stderr = sys.stderr
_stdout = io.StringIO()
_stderr = io.StringIO()
sys.stdout = _stdout
sys.stderr = _stderr
try:
    globals().update(json.loads(base64.b64decode('{ctx_b64}').decode('utf-8')))
except Exception as _e:
    print('context 注入失败:', _e, file=sys.stderr)
try:
{chr(10).join("    " + line for line in code.split(chr(10)))}
except Exception as e:
    print(traceback.format_exc(), file=sys.stderr)
finally:
    try:
        import matplotlib.pyplot as plt
        fig = plt.gcf()
        if fig and fig.get_axes():
            buf = io.BytesIO()
            fig.savefig(buf, format='png', dpi=100, bbox_inches='tight')
            buf.seek(0)
            img = base64.b64encode(buf.read()).decode()
            print('___MATPLOTLIB_IMG___' + img)
            plt.close('all')
    except Exception:
        pass
    _out = _stdout.getvalue()
    _err = _stderr.getvalue()
    sys.stdout = _old_stdout
    sys.stderr = _old_stderr
    print(_out, end='')
    if _err:
        print('___SANDBOX_STDERR___' + _err, file=sys.stderr)
'''

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
            f.write(wrapped_code)
            tmp_path = f.name

        proc = subprocess.Popen(
            [sys.executable, tmp_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env={**os.environ, 'MPLBACKEND': 'Agg'},
        )
        # P0-2：Windows 下用 Job Object 落实子进程内存限额 + 超时整树击杀
        _job = _apply_memory_limit(proc)
        try:
            stdout, stderr = proc.communicate(timeout=MAX_EXECUTION_TIME)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
            return {"ok": False, "error": f"执行超时（{MAX_EXECUTION_TIME}秒）", "stdout": "", "stderr": ""}
        finally:
            if _job:
                try:
                    import ctypes as _ct
                    _ct.windll.kernel32.CloseHandle(_job)
                except Exception:
                    pass

        output_type = "text"
        output = stdout

        # 检测 matplotlib 图片
        if '___MATPLOTLIB_IMG___' in stdout:
            idx = stdout.index('___MATPLOTLIB_IMG___')
            img_base64 = stdout[idx + len('___MATPLOTLIB_IMG___'):].strip()
            output = f"data:image/png;base64,{img_base64}"
            output_type = "image"
            stdout = stdout[:idx].strip()

        stderr_clean = stderr
        if '___SANDBOX_STDERR___' in stderr:
            idx = stderr.index('___SANDBOX_STDERR___')
            stderr_clean = stderr[idx + len('___SANDBOX_STDERR___'):].strip()
        if ctx_note:
            stderr_clean = (stderr_clean + ("\n" if stderr_clean else "")
                            + "[context 注入提示]\n" + ctx_note).strip()

        return {
            "ok": proc.returncode == 0,
            "stdout": stdout,
            "stderr": stderr_clean,
            "output_type": output_type,
            "output": output,
        }
    except Exception as e:
        return {"ok": False, "error": f"执行错误: {str(e)}", "stdout": "", "stderr": ""}
    finally:
        # P1-7：临时文件必须清理（此前 TimeoutExpired 时 os.unlink 被跳过导致泄漏）
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ==================== Windows Job Object 资源限制（P0-2） ====================

_JOB_MEMORY_LIMIT = MAX_MEMORY_MB * 1024 * 1024


def _apply_memory_limit(proc: 'subprocess.Popen') -> Optional[int]:
    """Windows：把子进程挂到带内存上限 + KILL_ON_JOB_CLOSE 的 Job Object。
    非 Windows 或失败时返回 None（不阻断执行）。"""
    if sys.platform != 'win32':
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ('BasicLimitInformation', JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ('IoInfo', IO_COUNTERS),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t),
            ]

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = (JOB_OBJECT_LIMIT_PROCESS_MEMORY
                                                 | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
        info.ProcessMemoryLimit = _JOB_MEMORY_LIMIT
        if not kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            return None
        if not kernel32.AssignProcessToJobObject(job, int(proc._handle)):
            kernel32.CloseHandle(job)
            return None
        return job  # 调用方负责 CloseHandle
    except Exception:
        return None


def execute_javascript(code: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
    """安全执行 JavaScript（Node.js）。

    P0-2 加固：Node 子进程与主进程同权限，字符串黑名单曾可被
    ``process['exit']`` / ``globalThis['pro'+'cess']`` 等手法绕过。现改为
    「归一化 token 扫描」：去注释后扫描危险标识符，并拦截方括号成员访问
    （阻断动态属性逃逸的主要通道）。命中即拒绝，不做运行时兜底。
    """
    import re as _re
    # 去掉行注释与块注释，防止注释内误报/注释外被拼接
    normalized = _re.sub(r'/\*.*?\*/', ' ', code, flags=_re.S)
    normalized = _re.sub(r'//[^\n]*', ' ', normalized)

    forbidden_tokens = [
        'require', 'process', 'globalThis', 'eval', 'Function',
        'import', 'child_process', 'Buffer', 'constructor', 'prototype',
        '__proto__', 'module', 'WebAssembly', 'fetch', 'XMLHttpRequest',
        'WebSocket', 'dgram', 'setImmediate',
    ]
    for token in forbidden_tokens:
        if _re.search(rf'\b{_re.escape(token)}\b', normalized):
            return {"ok": False, "error": f"禁止的操作: {token}"}

    # 方括号成员访问（obj['pro'+'cess'] / process['exit'] 的唯一通道）
    if _re.search(r"\[\s*['\"`]", normalized):
        return {"ok": False, "error": "禁止的操作: 方括号动态属性访问"}

    wrapped_code = f"""
(async () => {{
    try {{
        {code}
    }} catch (e) {{
        console.error('Error:', e.message);
        process.exit(1);
    }}
}})();
"""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.js', delete=False, encoding='utf-8') as f:
            f.write(wrapped_code)
            temp_path = f.name

        result = subprocess.run(
            ['node', temp_path],
            capture_output=True, text=True, timeout=MAX_EXECUTION_TIME,
            env={**os.environ, 'NODE_PATH': ''}
        )

        return {
            "ok": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "output_type": "text",
            "output": result.stdout,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"执行超时（{MAX_EXECUTION_TIME}秒）"}
    except FileNotFoundError:
        return {"ok": False, "error": "Node.js 未安装"}
    except Exception as e:
        return {"ok": False, "error": f"执行错误: {str(e)}"}
    finally:
        # P1-7：临时文件 try/finally 清理
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def execute_code(code: str, language: str = 'python', context: Dict[str, Any] = None) -> Dict[str, Any]:
    """执行代码入口"""
    if not code or not code.strip():
        return {"ok": False, "error": "代码为空"}
    if language.lower() in ('python', 'py'):
        return execute_python(code, context)
    elif language.lower() in ('javascript', 'js'):
        return execute_javascript(code, context)
    else:
        return {"ok": False, "error": f"不支持的语言: {language}"}
