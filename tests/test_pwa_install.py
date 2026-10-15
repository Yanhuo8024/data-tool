# -*- coding: utf-8 -*-
"""PWA 可安装性回归（2026-10-08）。

背景：`sw.js` 位于 `/static/` 下，未显式声明 scope → 注册作用域退化为 `/static/`。
      实测（Playwright + 系统 Edge）：
          getRegistration('/') === null      # 不覆盖 start_url
          navigator.serviceWorker.controller === null
      Chromium 因「SW 未覆盖 start_url」判定不可安装 → 「安装成应用」无从谈起，
      任务栏图标自然也不会出现。

踩坑记录（关键）：**`Service-Worker-Allowed` 只是放宽作用域上限，
      不传 scope 时默认作用域仍取脚本所在目录**。实测：
          register('/static/sw.js')                    → scope /static/   ← 仍然错
          register('/static/sw.js', {scope: '/'})      → scope /          ← 正确
      所以服务端响应头与前端显式 scope 必须**同时**到位。

另：作用域放宽到 `/` 后，SW 会开始看见 `/api/` 请求。若继续走
      stale-while-revalidate，接口会返回过期数据 —— 故 fetch 处理器必须放行动态请求。
"""
import io
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, 'app.py')
SW = os.path.join(ROOT, 'static', 'sw.js')
INDEX = os.path.join(ROOT, 'templates', 'index.html')


def _read(p):
    return io.open(p, encoding='utf-8', newline='').read()


class Test作用域声明:

    def test_服务端下发Service_Worker_Allowed(self):
        src = _read(APP)
        assert 'Service-Worker-Allowed' in src, 'app.py 必须对 sw.js 下发该响应头'
        i = src.index('Service-Worker-Allowed')
        assert "'/'" in src[i:i + 80], '作用域上限应为 /'

    def test_sw脚本本身不可长缓存(self):
        """sw.js 若被 immutable 长缓存，SW 更新永远不生效。"""
        src = _read(APP)
        i = src.index("request.path == '/static/sw.js'")
        block = src[i:i + 400]
        assert 'no-cache' in block or 'no-store' in block, 'sw.js 必须禁缓存'
        assert "pop('Expires'" in block or 'Expires' not in block

    def test_页面显式声明scope(self):
        html = _read(INDEX)
        i = html.index('serviceWorker.register')
        call = html[i:i + 160]
        assert "register('/static/sw.js'" in call
        assert re.search(r"scope:\s*'/'", call), \
            "必须显式 scope:'/' —— 只靠响应头，默认作用域仍是 /static/"

    def test_清理历史static作用域注册(self):
        """老用户残留的 /static/ 注册会继续用旧缓存覆盖图标，必须清理。"""
        html = _read(INDEX)
        assert 'getRegistrations' in html
        assert "indexOf('/static/')" in html
        assert 'unregister' in html


class Test不缓存动态请求:
    """作用域变宽后最容易引入的回归：接口被 stale-while-revalidate 缓存。"""

    def test_放行非静态非导航请求(self):
        sw = _read(SW)
        i = sw.index("addEventListener('fetch'")
        body = sw[i:i + 1400]
        assert '/api/' in body, 'fetch 处理器必须显式排除 /api/'
        assert 'isNav' in body and 'isStatic' in body

    def test_导航走网络优先(self):
        sw = _read(SW)
        assert 'isNavigate' in sw or "mode === 'navigate'" in sw

    def test_跨域不接管(self):
        sw = _read(SW)
        assert 'self.location.origin' in sw, 'CDN/字体等跨域资源不应被接管'

    def test_api不被写入缓存名单(self):
        sw = _read(SW)
        m = re.search(r'STATIC_ASSETS\s*=\s*\[(.*?)\]', sw, re.S)
        assert m, '未找到 STATIC_ASSETS'
        assert '/api/' not in m.group(1), '预缓存清单不应含接口'
