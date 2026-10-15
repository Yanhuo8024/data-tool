#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上传 → 文件列表 链路回归守卫。

背景（2026-10-03 修复「上传界面上传文件成功后没有显示」）：
用户看到的是「toast 提示上传成功，但页面始终不出现任何文件」。实测是三层缺陷叠加：

  ① 后端 `/api/files` 用 12 次递归 `glob('**')` 扫整棵输出目录树 —— 本机 975 个子目录
     / 2549 个文件下，进程内 4.0s、服务内 **9.8s**，上传后刷新列表要等十几秒；
  ② 前端 `renderFL()` 渲染的目标是 `#db-files` —— 该 id 在整个项目里**不存在**
     （仪表盘那张卡片早已从模板移除）→ `getElementById` 返回 null 后直接 return，
     「已导入文件」区（`#fl`）永远空白、标题 `#fl-title` 永远 `display:none`；
  ③ `loadDashboard()` 无条件写 `#db-file-count`（同样不存在）→ 抛 TypeError 中断函数，
     其后的 `renderFL(data.files)` 永不执行 → 首屏文件列表也为空。

本文件只做**可确定性判定**的守卫（结构 / 接线 / 隔离），不依赖外网。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FILE_ROUTES = ROOT / 'routes' / 'file_routes.py'
CORE_JS = ROOT / 'static' / 'js' / 'core.js'
DATA_JS = ROOT / 'static' / 'js' / 'data-analysis.js'
UPLOAD_TAB = ROOT / 'templates' / 'tabs' / 'tab-upload.html'

ALLOWED_EXTS = {'.csv', '.xlsx', '.xls', '.json', '.txt', '.md',
                '.html', '.py', '.png', '.jpg', '.svg', '.xml'}


# ── 工具 ──────────────────────────────────────────────────────

def _api_files_src() -> str:
    """截取 `api_files` 这一个处理函数的源码（到下一个路由装饰器为止）。"""
    src = FILE_ROUTES.read_text(encoding='utf-8')
    i = src.index('def api_files()')
    j = src.index('@bp.route', i)
    return src[i:j]


def _renderfl_src() -> str:
    src = CORE_JS.read_text(encoding='utf-8')
    i = src.index('function renderFL(')
    j = src.index('\nfunction ', i + 1)
    return src[i:j]


def _ids_in(path: Path) -> set:
    """模板里出现过的所有 id 字面量。"""
    return set(re.findall(r'id="([^"]+)"', path.read_text(encoding='utf-8')))


# ── ① 后端：单次遍历取代 12 次递归 glob ────────────────────────

class TestApiFilesTraversal:

    def test_只做一次目录遍历(self):
        body = _api_files_src()
        assert 'os.walk(' in body, 'api_files 应使用单次 os.walk 遍历'
        assert 'glob.glob(' not in body, 'api_files 不得再按扩展名逐个递归 glob'

    def test_不再吞异常(self):
        body = _api_files_src()
        # R2 口径：except 体只有 pass/continue 视为吞异常；此处必须留痕
        assert not re.search(r'except[^\n]*:\s*\n\s*(pass|continue)\s*\n', body), \
            'api_files 不应存在静默吞异常'
        assert 'logger.debug' in body, 'stat 失败应记录 logger.debug'

    def test_扩展名白名单覆盖原有12种(self):
        body = _api_files_src()
        m = re.search(r'exts\s*=\s*\{([^}]+)\}', body)
        assert m, 'api_files 应有扩展名集合'
        got = {x.strip().strip("'\"") for x in m.group(1).split(',') if x.strip()}
        assert got == ALLOWED_EXTS, f'扩展名白名单变了: {got ^ ALLOWED_EXTS}'

    def test_endpoint_不再调用_glob(self, client, monkeypatch):
        """行为级守卫：把 glob.glob 换成会记账的桩，端点应完全不碰它。"""
        import glob as _glob
        calls = []

        def _spy(*a, **k):
            calls.append(a)
            return []

        monkeypatch.setattr(_glob, 'glob', _spy)
        resp = client.get('/api/files')
        assert resp.status_code == 200
        assert calls == [], f'api_files 又用上了 glob（{len(calls)} 次调用）'


class TestApiFilesContract:

    def test_返回结构(self, client):
        resp = client.get('/api/files')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['ok'] is True
        assert isinstance(data['files'], list)
        assert isinstance(data.get('deleted'), list)
        for f in data['files']:
            assert set(f) >= {'name', 'path', 'size', 'mtime', 'ext', 'tags'}
            assert f['ext'] == Path(f['name']).suffix.lower()
        # 列表按 mtime 倒序（前端「最近文件」依赖此约定）
        mts = [f['mtime'] for f in data['files']]
        assert mts == sorted(mts, reverse=True)

    def test_不暴露隐藏文件与内部元数据(self, client):
        """隐藏文件/目录不进列表：否则 .deleted_files.json 等内部文件会出现在上传页。"""
        for f in client.get('/api/files').get_json()['files']:
            assert not Path(f['name']).name.startswith('.'), f'暴露了隐藏文件 {f["name"]}'
            assert '.git' not in f['path']

    def test_过滤逻辑对临时目录生效(self, client, monkeypatch):
        """把 OUT_DIR 指到临时目录，逐条验证过滤规则（无需触碰真实 output/）。"""
        import routes.file_routes as fr
        tmp = Path(pytest.importorskip('tempfile').mkdtemp(prefix='cdfl_'))
        (tmp / 'keep.csv').write_text('a\n1\n', encoding='utf-8')
        (tmp / 'sub').mkdir()
        (tmp / 'sub' / 'nested.xlsx').write_text('x', encoding='utf-8')
        (tmp / 'note.py').write_text('x=1\n', encoding='utf-8')
        (tmp / '.hidden.csv').write_text('a\n1\n', encoding='utf-8')          # 隐藏文件 → 排除
        (tmp / '.deleted_b.csv').write_text('a\n1\n', encoding='utf-8')       # 软删除标记 → 排除
        (tmp / 'skip.log').write_text('x', encoding='utf-8')                  # 扩展名不在白名单
        (tmp / '.secret').mkdir()
        (tmp / '.secret' / 'leak.csv').write_text('a\n1\n', encoding='utf-8')  # 隐藏目录 → 不入

        monkeypatch.setattr(fr, 'OUT_DIR', tmp)
        names = {f['name'] for f in client.get('/api/files').get_json()['files']}
        assert names == {'keep.csv', 'nested.xlsx', 'note.py'}, f'过滤结果不符: {names}'


# ── ② 前端：renderFL 必须写进真实存在的容器 ─────────────────────

class TestRenderFlWiring:

    def test_不再只写已移除的_db_files(self):
        body = _renderfl_src()
        assert "'fl'" in body, 'renderFL 必须把内容写进上传页的 #fl'
        assert "'db-files'" in body  # 兼容分支保留，但不能再是唯一目标

    def test_渲染目标都在模板里存在(self):
        src = _renderfl_src()
        # 收集数组里的 id 字面量（渲染目标表）
        m = re.search(r"\[\s*((?:'[^']+'\s*,?\s*)+)\]", src)
        assert m, '找不到 renderFL 的容器数组'
        ids = set(re.findall(r"'([^']+)'", m.group(1)))
        upload_ids = _ids_in(UPLOAD_TAB)
        assert 'fl' in ids and 'fl' in upload_ids, '#fl 必须在渲染目标里且存在于上传页模板'
        assert 'fl-title' in upload_ids, '#fl-title 必须存在于上传页模板'

    def test_空列表时给出占位而不是留白(self):
        body = _renderfl_src()
        assert '暂无数据文件' in body

    def test_标题随有无文件切换(self):
        body = _renderfl_src()
        assert 'fl-title' in body and 'style.display' in body


class TestUploadChainWiring:

    def test_loadDashboard_对缺失计数元素判空(self):
        src = CORE_JS.read_text(encoding='utf-8')
        # 直接 .textContent 赋值会抛 TypeError 并中断整个函数（连带 renderFL 不执行）
        assert "getElementById('db-file-count').textContent" not in src, \
            'loadDashboard 仍在无条件写 #db-file-count'
        assert "getElementById('db-model-count').textContent" not in src, \
            'loadDashboard 仍在无条件写 #db-model-count'
        assert 'if (cntEl) cntEl.textContent = count;' in src
        assert 'if (mdlEl) mdlEl.textContent = mCount;' in src

    def test_fsChange_上传后立刻给出可见反馈(self):
        src = DATA_JS.read_text(encoding='utf-8')
        i = src.index('function fsChange(')
        body = src[i:src.index('\nfunction ', i + 1)]
        assert "getElementById('fl')" in body, 'fsChange 应在列表刷新前先把结果写进 #fl'
        assert '正在上传' in body and '正在刷新文件列表' in body
        assert '上传成功' in body and '上传失败' in body, '不应再是英文 Upload success/failed'
        assert "input.value = ''" in body, '未清空 input，重复选同一文件不会再触发 change'

    def test_上传页容器齐备(self):
        ids = _ids_in(UPLOAD_TAB)
        for need in ('uz', 'fi', 'fl', 'fl-title', 'file-selector', 'file-list'):
            assert need in ids, f'上传页缺少容器 #{need}'
