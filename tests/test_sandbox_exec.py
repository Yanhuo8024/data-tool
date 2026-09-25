"""sandbox_exec 代码执行沙箱测试（AST 静态审查）"""
import pytest

from sandbox_exec import check_code_safety, execute_code


class TestCheckCodeSafety:
    """check_code_safety：AST 审查（复用 plugin_sandbox 策略）"""

    def test_benign_data_code_passes(self):
        code = (
            "import pandas as pd\n"
            "import numpy as np\n"
            "df = pd.DataFrame({'a': [1, 2, 3]})\n"
            "print(df['a'].mean())\n"
        )
        assert check_code_safety(code) is None

    def test_import_os_rejected(self):
        assert check_code_safety("import os\nos.system('dir')") is not None

    def test_import_from_os_rejected(self):
        assert check_code_safety("from os import system") is not None

    def test_dunder_import_bypass_rejected(self):
        # 旧字符串黑名单查 'import os'，可被 __import__ 绕过；AST 审查必须拦截
        assert check_code_safety("os = __import__('os')\nos.system('x')") is not None

    def test_exec_concat_bypass_rejected(self):
        assert check_code_safety("exec('import os')") is not None

    def test_dunder_escape_rejected(self):
        code = "(lambda: [c for c in ().__class__.__base__.__subclasses__()])()"
        assert check_code_safety(code) is not None

    def test_builtins_escape_rejected(self):
        assert check_code_safety("__builtins__") is not None

    def test_open_rejected(self):
        assert check_code_safety("f = open('data/secret.key')") is not None

    def test_syntax_error_passes_through(self):
        # 语法错误不在静态审查阶段拦截，由子进程给出带行号报错
        assert check_code_safety("def broken(:\n    pass") is None


class TestExecuteCode:
    """execute_code 入口行为"""

    def test_empty_code(self):
        r = execute_code("   ")
        assert r["ok"] is False and "空" in r["error"]

    def test_unsupported_language(self):
        r = execute_code("print(1)", language="ruby")
        assert r["ok"] is False and "ruby" in r["error"]

    # 2026-09-17：本用例只执行本地 sandbox_exec（子进程 + Job Object），不需要网络，
    # 原 @pytest.mark.network 属误标，会让沙箱核心功能失去默认覆盖（已移除）。
    def test_python_hello(self):
        r = execute_code("print('hello')")
        assert r["ok"] is True and "hello" in r["stdout"]

    def test_dangerous_code_blocked_without_subprocess(self):
        r = execute_code("import subprocess; subprocess.run(['dir'])")
        assert r["ok"] is False
        assert "禁止" in r["error"]
