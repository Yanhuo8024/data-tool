#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""「使用教程」页回归守卫。

2026-10-03 改版要点（本文件守住的就是这些契约）：
  · 形态：手册式 —— 左侧粘性目录 + 关键词搜索 + 滚动高亮 + 每模块可折叠；
  · 内容：每个模块都要有「怎么做」（操作步骤）与「容易踩的坑」；
  · 覆盖：导航里的**每一个页签**都要在教程里有对应条目（漏一个就是"教程不全"）；
  · 接线：所有「打开」按钮指向的页签必须真实存在（本项目出现过跳转到已删页签的问题）；
  · 死代码：core.js 里那套查询 .tut-toc / .tut-mod 的旧实现必须已删除。

这些都是纯静态可判定的，不依赖浏览器。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HELP = ROOT / 'templates' / 'tabs' / 'tab-help.html'
INDEX = ROOT / 'templates' / 'index.html'
CORE_JS = ROOT / 'static' / 'js' / 'core.js'
TUT_JS = ROOT / 'static' / 'js' / 'tutorial.js'
TEMPLATES = ROOT / 'templates'

SELF_TAB = 'tab-help'          # 教程页自己不需要给自己写条目


def _help() -> str:
    return HELP.read_text(encoding='utf-8')


def _nav_tabs() -> set:
    """左侧导航里真实存在的页签 id 集合。"""
    return set(re.findall(r'data-t="(tab-[a-z0-9-]+)"', INDEX.read_text(encoding='utf-8')))


def _tab_containers() -> set:
    """模板中真实渲染出来的页签容器 id 集合。"""
    found = set()
    for f in TEMPLATES.rglob('*.html'):
        found |= set(re.findall(r'id="(tab-[a-z0-9-]+)"', f.read_text(encoding='utf-8')))
    return found


def _modules() -> list:
    """教程里全部模块块的 HTML 片段（按 <details class="tm"> 切分）。"""
    src = _help()
    parts = re.split(r'<details class="tm"', src)[1:]
    return [p.split('</details>')[0] for p in parts]


def _module_id(block: str) -> str:
    m = re.search(r'id="([a-z0-9-]+)"', block)
    return m.group(1) if m else '?'


def _switch_targets(block: str) -> list:
    return re.findall(r"switchToTab\('([a-z0-9-]+)'\)", block)


# ── 覆盖完整性 ─────────────────────────────────────────────

class TestCoverage:
    """教程必须覆盖全部功能页签 —— 这是「完善教程」的可执行判据。"""

    def test_每个导航页签都有对应条目(self):
        nav = _nav_tabs() - {SELF_TAB}
        covered = set()
        for blk in _modules():
            covered |= set(_switch_targets(blk))
        missing = sorted(nav - covered)
        assert not missing, f'教程未覆盖这些页签: {missing}'

    def test_条目数量与页签数量一致(self):
        nav = _nav_tabs() - {SELF_TAB}
        mods = _modules()
        assert len(mods) == len(nav), f'模块条目 {len(mods)} 个 vs 应覆盖页签 {len(nav)} 个'

    def test_没有重复或多余的条目(self):
        ids = [_module_id(b) for b in _modules()]
        assert len(ids) == len(set(ids)), f'模块 id 有重复: {ids}'
        targets = []
        for b in _modules():
            targets += _switch_targets(b)
        dup = sorted({t for t in targets if targets.count(t) > 1})
        assert not dup, f'多个条目指向同一页签: {dup}'


# ── 接线正确性 ─────────────────────────────────────────────

class TestWiring:

    def test_打开按钮的目标页签都真实存在(self):
        bad = []
        for blk in _modules():
            for t in _switch_targets(blk):
                if t not in _tab_containers():
                    bad.append((_module_id(blk), t))
        assert not bad, f'指向了不存在的页签: {bad}'

    def test_每个模块都有打开入口(self):
        no_btn = [_module_id(b) for b in _modules() if not _switch_targets(b)]
        assert not no_btn, f'这些模块缺少「打开」按钮: {no_btn}'

    def test_目录与正文一一对应(self):
        src = _help()
        gids = set(re.findall(r'class="tgroup" id="([a-z0-9-]+)"', src))
        toc_g = set(re.findall(r'data-g="([a-z0-9-]+)"', src))
        assert toc_g == gids, f'目录分组 {sorted(toc_g)} 与正文分组 {sorted(gids)} 不一致'

        anchors = set(re.findall(r'class="tma" href="#([a-z0-9-]+)"', src))
        mod_ids = {_module_id(b) for b in _modules()}
        assert anchors == mod_ids, f'目录模块链接与正文模块 id 不一致: {sorted(anchors ^ mod_ids)}'

    def test_页面容器齐备(self):
        src = _help()
        for need in ('id="tut-toc"', 'id="tut-doc"', 'id="tut-search"',
                     'id="tut-expand"', 'id="tut-collapse"', 'id="tut-empty"', 'id="tut-count"'):
            assert need in src, f'教程页缺少 {need}'

    def test_脚本已挂载且旧实现已删除(self):
        assert 'js/tutorial.js' in INDEX.read_text(encoding='utf-8'), 'index.html 未引入 tutorial.js'
        assert TUT_JS.exists(), 'static/js/tutorial.js 不存在'
        core = CORE_JS.read_text(encoding='utf-8')
        assert 'function initTutHelp' not in core, 'core.js 里仍有已失效的 initTutHelp'
        assert 'function tutToggleChapter' not in core, 'core.js 里仍有已失效的 tutToggleChapter'
        assert 'TutorialUI.init' in core, 'tab-help 的初始化应委托给 TutorialUI.init()'


# ── 内容深度（用户选定的粒度：步骤 + 字段 + 坑）──────────────

class TestContentDepth:

    def test_每个模块都有操作步骤(self):
        bad = [_module_id(b) for b in _modules() if '怎么做' not in b]
        assert not bad, f'这些模块缺少「怎么做」: {bad}'

    def test_每个模块都有常见坑(self):
        bad = [_module_id(b) for b in _modules() if 'class="tsub pit"' not in b]
        assert not bad, f'这些模块缺少「容易踩的坑」: {bad}'

    def test_每个模块至少两个小节(self):
        bad = [_module_id(b) for b in _modules() if b.count('class="tsub') < 2]
        assert not bad, f'这些模块内容过薄（少于 2 个小节）: {bad}'

    def test_搜索关键词非空(self):
        bad = []
        for blk in _modules():
            m = re.search(r'data-kw="([^"]*)"', blk)
            if not m or len(m.group(1).strip()) < 4:
                bad.append(_module_id(blk))
        assert not bad, f'这些模块缺少足够的搜索关键词: {bad}'

    def test_不出现已核实为错的说法(self):
        """「存储页可加载示例数据」经代码核实并不存在（存储页只有入库/下载/血缘/分享）。"""
        src = _help()
        assert '示例数据可在' not in src, '教程又写回了并不存在的「存储页加载示例数据」'


# ── 关键事实与代码一致 ─────────────────────────────────────

class TestFactsMatchCode:

    @pytest.mark.parametrize('phrase', [
        '0.0.0.0',        # 局域网访问依赖绑定 0.0.0.0
        'Ctrl+F5',        # 前端改动需硬刷新
        'Ctrl+K',         # 全局搜索
        '涨红跌绿',        # A 股配色约定
    ])
    def test_关键事实保留(self, phrase):
        assert phrase in _help(), f'教程丢失了关键事实：{phrase}'

    def test_快捷键与实现一致(self):
        core = CORE_JS.read_text(encoding='utf-8')
        assert 'Ctrl+Shift+1~9' in core, '快捷键帮助表变了，教程需同步'
        src = _help()
        assert 'Ctrl+Shift+1~9' in src, '教程里的快捷键说明与 core.js 的帮助表不一致'


# ── 帮助内容不得重复散落（2026-10-03 合并「智能流程」页内嵌教程）──────

GUIDE = ROOT / 'templates' / 'tabs' / 'tab-guide.html'


class TestNoFragmentedHelp:
    """帮助内容只应有一个家 = 使用教程页。

    背景：tab-guide.html 的文件头注释原本写着「智能流程推荐 + 使用教程」——在有独立
    教程页之前，4 块教程（企业深析手把手 / 数据源导航 / AI 助手能问什么 / FAQ）是内嵌在
    这一页里的。后来加了教程页却没人把旧内容搬走，于是同一件事写在两处、还各写一半
    （该页 229 行里 131 行是教程）。现已合并，本类防止它再长回来。
    """

    @pytest.mark.parametrize('title', [
        '手把手教程', '数据源导航', '能问什么', '常见问题（FAQ）',
    ])
    def test_智能流程页不再内嵌教程块(self, title):
        assert title not in GUIDE.read_text(encoding='utf-8'), \
            f'「智能流程」页又出现了教程块：{title}'

    def test_智能流程页仍保留自身功能(self):
        src = GUIDE.read_text(encoding='utf-8')
        for need in ('gd-flow-area', 'recommendFlow()', 'quickFlow('):
            assert need in src, f'智能流程页丢了自身功能：{need}'

    def test_教程页承接了迁移过来的独有内容(self):
        src = _help()
        for kw in ('巨潮', '企查查', '裁判文书', '投研备忘录', '商务深蓝',
                   'localStorage', '六大分类'):
            assert kw in src, f'教程页缺少从智能流程页迁来的内容：{kw}'

    def test_教程页有常见问题分组(self):
        src = _help()
        assert 'class="tgroup" id="gfaq"' in src, '教程页缺少常见问题分组'
        assert src.count('<details class="tqi"') >= 5, '常见问题条目过少'


class TestDeadHelpCode:
    """walkthrough.js 自称「用于使用教程页」，但它需要的 8 个 DOM（#wt-title 等）
    在任何模板里都不存在，也没有任何地方调用 startWalkthrough() —— 纯死代码。"""

    def test_walkthrough_js_已删除(self):
        assert not (ROOT / 'static' / 'js' / 'walkthrough.js').exists(), 'walkthrough.js 应已删除'

    def test_index_不再引用_walkthrough(self):
        assert 'walkthrough.js' not in INDEX.read_text(encoding='utf-8')


class TestPageHygiene:

    def test_同一脚本不重复引入(self):
        """本次改版真实踩过：替换 <script> 行时把 tutorial.js 引了两次。
        重复引入会让 IIFE 执行两遍、事件被重复绑定。"""
        srcs = re.findall(r'<script src="([^"?]+)', INDEX.read_text(encoding='utf-8'))
        dup = sorted({s for s in srcs if srcs.count(s) > 1})
        assert not dup, f'index.html 重复引入脚本: {dup}'
