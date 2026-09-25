# -*- coding: utf-8 -*-
"""conftest.py — data.tool 测试全局配置

职责：
  1. 把项目根目录加入 sys.path（pytest 自动按 rootdir 推断，但便携版 Python
     在 ._pth 模式下不一定能正确识别 rootdir，所以显式兜底）。
  2. 准备测试环境变量：禁用限流、关闭网络调用、设 SECRET_KEY、设默认管理员密码。
  3. 提供共享 fixtures：app/client/admin_token。
  4. 把修复版 ai_assistant.py 的 PROFILE_IMPORTS 探针关掉（避免控制台噪音）。
"""
import os
import sys
import tempfile
import warnings
from pathlib import Path

# 1. sys.path 兜底
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# 2. 测试环境变量（在导入 app 之前设置）
os.environ.setdefault('TESTING', '1')
os.environ.setdefault('RATELIMIT_ENABLED', '0')
os.environ.setdefault('PROFILE_IMPORTS', '0')          # 关掉启动期 import 诊断探针
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
os.environ.setdefault('JWT_SECRET_KEY', 'test-jwt-secret')
os.environ.setdefault('DATABASE_URL', f'sqlite:///{_PROJECT_ROOT}/data/test_app.db')
os.environ.setdefault('ADMIN_USERNAME', 'admin')
os.environ.setdefault('ADMIN_PASSWORD', 'admin123')
os.environ.setdefault('NO_PROXY', '*')
os.environ.setdefault('DISABLE_SCHEDULER', '1')

# 5. 测试轨迹隔离（2026-09-18）：pytest 运行会把 agent 轨迹写进 trace_store；
# 不设隔离时全部落到生产目录 output/traces/，测试桩抛的 TypeError 被管线兜底
# 记成"阶段失败"，把健康面板的 PIPELINE_STAGE_FAILED 刷成 12% 虚高（全是假警报）。
# 强制指到临时目录（注意不能用 setdefault——要让外部 TRACE_DIR 也失效）。
os.environ['TRACE_DIR'] = tempfile.mkdtemp(prefix='dt-test-traces-')

# 6. 用户数据隔离（2026-09-19，P0）
# 事故：多个测试直接操作 ai_assistant._chat_manager 与 conversation_memory 全局单例
# （如 tests/test_nlq_routing_and_pipeline_observability.py 里的 history.clear()），
# 而这两个模块都在 atexit 注册了"把内存回写磁盘"，落点是与生产共用的固定路径：
#   ai_assistant.HISTORY_PATH        -> data/chat_history.json
#   conversation_memory._MEMORY_FILE -> data/conversation_memory.json
# 于是「跑一次全量回归」= 把真实聊天记录清空，服务重启后历史凭空消失
# （用户可见症状：AI 助手与右栏「最近对话」全都空白）。
# 这里把两个落点永久重定向到临时目录：必须在导入这些模块之前改，且
# 不能依赖 monkeypatch（它在测试结束就还原，而 atexit 在进程退出时才执行，
# 还原后会照样写回生产文件）。因此直接改成模块属性，不注册还原。
_TEST_DATA_DIR = tempfile.mkdtemp(prefix='dt-test-data-')
os.environ['DT_TEST_DATA_DIR'] = _TEST_DATA_DIR
# 聊天记录本地归档（data/chat_history/YYYY-MM-DD/）同样必须隔离：
# _save_history() 每次落盘都会调 _sync_chat_archive()，不隔离则跑一次测试
# 就往生产归档目录里写测试桩对话（与 2026-09-19 清空生产记录同源风险）。
os.environ['DT_CHAT_ARCHIVE_DIR'] = os.path.join(_TEST_DATA_DIR, 'chat_history')
try:
    import chat_archive as _ca_iso
    _ca_iso.ARCHIVE_DIR = os.path.join(_TEST_DATA_DIR, 'chat_history')
except Exception:
    pass
try:
    import ai_assistant as _aa_iso
    _aa_iso.HISTORY_PATH = os.path.join(_TEST_DATA_DIR, 'chat_history.json')
except Exception:  # 模块暂不可导入时静默（个别测试会自行 monkeypatch 路径）
    pass
try:
    import conversation_memory as _cm_iso
    _cm_iso._MEMORY_FILE = Path(_TEST_DATA_DIR) / 'conversation_memory.json'
except Exception:
    pass

# 3. 抑制无害 warning
warnings.filterwarnings('ignore', category=DeprecationWarning)
warnings.filterwarnings('ignore', category=PendingDeprecationWarning)
warnings.filterwarnings('ignore', category=UserWarning)
warnings.filterwarnings('ignore', category=FutureWarning)

# 4. 共享 fixtures（被各测试文件 import 使用）
import pytest


def _wait_for_lazy_routes(application, timeout: float = 15.0):
    """等 routes 后台注册线程完成（P0-5 改造带来的）。
    routes 模块的 _ALL_DONE=True 表示所有 lazy 路由已注册。
    最长等 timeout 秒（默认 15）。"""
    import time as _t
    try:
        import routes as _routes_mod
    except Exception:
        return
    deadline = _t.monotonic() + timeout
    while not getattr(_routes_mod, '_ALL_DONE', True):
        if _t.monotonic() > deadline:
            break
        _t.sleep(0.1)


@pytest.fixture(scope='session')
def app():
    """返回已配置好的 Flask app 实例（session 级别，加速）。"""
    try:
        import app as app_mod
    except Exception as e:
        pytest.skip(f'无法导入 app 模块：{e}')
    application = app_mod.app
    application.config['TESTING'] = True
    application.config['RATELIMIT_ENABLED'] = False
    application.config['SECRET_KEY'] = 'test-secret-key'
    # 等所有 lazy 路由注册完成（routes 分阶段加载机制）
    _wait_for_lazy_routes(application, timeout=60.0)
    import routes as _r
    import os as _o
    _o.environ.setdefault('PYTEST_DEBUG_WAIT', '0')
    if _o.environ.get('PYTEST_DEBUG_WAIT') == '1':
        print(f'\n[conftest] app fixture ready: routes={len(list(application.url_map.iter_rules()))}, _ALL_DONE={_r._ALL_DONE}\n')
    return application


@pytest.fixture(scope='session')
def client(app):
    """Flask test_client（session 级别）。"""
    return app.test_client()


@pytest.fixture(scope='session')
def admin_token(client):
    """登录 admin / admin123 拿 token。失败时返回 None 让测试自行决定。"""
    try:
        resp = client.post('/api/auth/login',
                           json={'username': 'admin', 'password': 'admin123'})
        if resp.status_code == 200:
            data = resp.get_json() or {}
            return data.get('token') or data.get('access_token')
    except Exception:
        pass
    return None


@pytest.fixture
def admin_headers(admin_token):
    """带 Authorization 的 header 字典。"""
    if not admin_token:
        return {}
    return {'Authorization': f'Bearer {admin_token}'}