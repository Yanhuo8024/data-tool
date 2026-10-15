# -*- coding: utf-8 -*-
"""品牌图标 / PWA 清单回归（2026-10-08）。

背景：网站三个 PNG 图标曾是**纯色空白占位块**（rgb 85,96,191，无任何图形），
      而 favicon.svg、侧边栏品牌标识又是另外两种图形 —— 三套标识互不一致。
      这类故障是**静默**的：页面照常打开，只有安装成应用后任务栏里能看到异常。

本文件锁死：
  ① 图标文件齐全、格式/尺寸正确；
  ② **图标不是空白块**（防止占位图再次混入）；
  ③ manifest 的 any / maskable 分离正确（旧版把同一张图同时标 any+maskable）；
  ④ maskable 图形落在 80% 安全区内；
  ⑤ 页面与 Service Worker 都已引用，且缓存版本已升（否则老用户看到旧图标）。
"""
import io
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, 'static')
INDEX = os.path.join(ROOT, 'templates', 'index.html')
SW = os.path.join(STATIC, 'sw.js')
MANIFEST = os.path.join(STATIC, 'manifest.json')

PIL = pytest.importorskip('PIL', reason='Pillow 未安装')
from PIL import Image  # noqa: E402

# 期望的资产：文件名 -> (宽, 高)
PNG_ASSETS = {
    'favicon-16.png': (16, 16),
    'favicon-32.png': (32, 32),
    'apple-touch-icon.png': (180, 180),
    'icon-192.png': (192, 192),
    'icon-512.png': (512, 512),
    'icon-maskable-192.png': (192, 192),
    'icon-maskable-512.png': (512, 512),
}


def _read(path):
    return io.open(path, encoding='utf-8', newline='').read()


def _icon(name):
    p = os.path.join(STATIC, name)
    assert os.path.exists(p), f'图标缺失: {name}'
    return Image.open(p).convert('RGBA')


class Test资产齐全:

    @pytest.mark.parametrize('name,size', sorted(PNG_ASSETS.items()))
    def test_png存在且尺寸正确(self, name, size):
        assert _icon(name).size == size

    def test_svg母版存在(self):
        for n in ('logo.svg', 'favicon.svg'):
            assert os.path.exists(os.path.join(STATIC, n)), n
            assert '<svg' in _read(os.path.join(STATIC, n))

    def test_ico存在且含多尺寸(self):
        p = os.path.join(STATIC, 'favicon.ico')
        assert os.path.exists(p)
        sizes = set(Image.open(p).info.get('sizes', []))
        for want in ((16, 16), (24, 24), (32, 32), (48, 48)):
            assert want in sizes, f'ICO 缺 {want}（Windows 任务栏/标题栏会取这几个尺寸）'


class Test图标不是空白块:
    """核心回归：占位空白图标曾长期存在且无人察觉。"""

    @pytest.mark.parametrize('name', sorted(PNG_ASSETS))
    def test_有图形内容(self, name):
        im = _icon(name)
        colors = im.getcolors(maxcolors=1 << 20) or []
        # 空白占位块只有极少数颜色（实测仅 1 种纯色）
        assert len(colors) >= 8, f'{name} 疑似空白占位图，仅 {len(colors)} 种颜色'

    @pytest.mark.parametrize('name', sorted(PNG_ASSETS))
    def test_含近白前景(self, name):
        """图形为白色柱状，必须存在高亮度像素，否则说明没渲染出 logo。"""
        im = _icon(name).resize((64, 64))
        bright = sum(1 for r, g, b, a in im.getdata()
                     if a > 200 and r > 235 and g > 235 and b > 235)
        assert bright >= 20, f'{name} 缺少白色图形像素（{bright}）'

    def test_圆角块有透明角(self):
        """any 类图标是圆角块，四角应为透明（蒙版才不会被方角顶破）。"""
        im = _icon('icon-512.png')
        assert im.getpixel((0, 0))[3] == 0
        assert im.getpixel((511, 511))[3] == 0
        assert im.getpixel((256, 256))[3] == 255


class TestMaskable安全区:

    @pytest.mark.parametrize('name', ['icon-maskable-192.png', 'icon-maskable-512.png'])
    def test_全幅不透明(self, name):
        """maskable 必须全幅铺底，不能有透明角。"""
        im = _icon(name)
        for pt in ((0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1)):
            assert im.getpixel(pt)[3] == 255, f'{name} 角落透明，遮罩会露底'

    @pytest.mark.parametrize('name', ['icon-maskable-192.png', 'icon-maskable-512.png'])
    def test_图形落在安全圈内(self, name):
        """maskable 规范：内容须落在直径 80% 的圆内，否则被裁。"""
        im = _icon(name)
        w, h = im.size
        xs, ys = [], []
        for y in range(h):
            for x in range(w):
                r, g, b, a = im.getpixel((x, y))
                if a > 200 and r > 235 and g > 235 and b > 235:   # 白色柱
                    xs.append(x)
                    ys.append(y)
        assert xs, f'{name} 未找到图形'
        # 距中心最远的图形像素
        cx, cy = (w - 1) / 2, (h - 1) / 2
        far = max(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 for x, y in zip(xs, ys))
        limit = 0.40 * w
        assert far <= limit, f'{name} 图形超出安全区：{far:.1f} > {limit:.1f}'


class Test清单与引用:

    def test_manifest是合法JSON且字段完整(self):
        m = json.loads(_read(MANIFEST))
        for k in ('name', 'short_name', 'start_url', 'display',
                  'theme_color', 'background_color', 'icons'):
            assert k in m, f'manifest 缺字段 {k}'
        assert m['display'] == 'standalone'

    def test_any与maskable分离(self):
        """旧版把同一张图同时标成 any maskable —— 会被系统裁掉图形。"""
        m = json.loads(_read(MANIFEST))
        any_icons = [i for i in m['icons'] if i['purpose'] == 'any']
        mask_icons = [i for i in m['icons'] if i['purpose'] == 'maskable']
        assert any_icons and mask_icons, 'manifest 必须同时提供 any 与 maskable'
        assert not [i for i in m['icons'] if 'maskable' in i['purpose'] and 'any' in i['purpose']], \
            '存在 any+maskable 混用的条目'
        assert {i['sizes'] for i in any_icons} >= {'192x192', '512x512'}
        assert {i['sizes'] for i in mask_icons} >= {'192x192', '512x512'}

    def test_manifest声明的文件都存在(self):
        m = json.loads(_read(MANIFEST))
        for i in m['icons']:
            p = os.path.join(ROOT, i['src'].lstrip('/').replace('/', os.sep))
            assert os.path.exists(p), f"manifest 指向的文件不存在: {i['src']}"

    def test_页面声明了图标与清单(self):
        html = _read(INDEX)
        assert 'favicon.svg' in html and 'favicon.ico' in html
        assert 'apple-touch-icon.png' in html
        assert 'rel="manifest"' in html

    def test_侧边栏品牌标识为柱状图(self):
        """站内品牌标识必须与图标同源，避免"三套标识"回潮。"""
        html = _read(INDEX)
        i = html.index('id="i-logo"')
        block = html[i:html.index('</symbol>', i)]
        assert block.count('<path') == 3, '品牌标识应为三根柱'
        assert 'fill="currentColor"' in block

    def test_sw已缓存新图标并升版本(self):
        sw = _read(SW)
        for n in ('favicon.ico', 'favicon-32.png', 'icon-maskable-192.png',
                  'icon-maskable-512.png'):
            assert n in sw, f'sw.js 未缓存 {n}'
        ver = int([t for t in sw.split("'") if t.startswith('datatool-v')][0].split('-v')[1])
        assert ver >= 10, '图标变更必须升 Service Worker 缓存版本，否则老用户看到旧图标'
