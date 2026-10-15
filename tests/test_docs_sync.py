#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档一致性守卫：文档里声明的规模数字必须与代码现状一致，镜像不许漂移。

背景（2026-10-03）：`design/功能介绍_大白话版.md` 与 `design/简历项目介绍_金融数据分析平台.md`
是给面试用的材料，里面的规模数字**天然会随代码更新而过时**——实测发现 09-25 版写的还是
"39 标签页 / 916 测试 / 32 工具 / 7 分组"，而现状已是 **40 页签 / 1079 测试 / 42 工具 / 10 分组**。
一份"数字对不上代码"的面试材料是负资产（被追问就露馅），所以把这几条做成断言。

只守**能静态数出来**的指标（页签、分组、工具装饰器、文档自身的章节数），
路由数/测试数需要运行时或 pytest 收集，不做静态断言（避免误报）。
"""
import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / 'design'
MIRROR = ROOT / 'github_upload' / 'design'
INDEX = ROOT / 'templates' / 'index.html'
TOOL_REGISTRY = ROOT / 'agent_tool_registry.py'

PLAIN = DESIGN / '功能介绍_大白话版.md'
RESUME = DESIGN / '简历项目介绍_金融数据分析平台.md'


def _read(p: Path) -> str:
    return p.read_text(encoding='utf-8')


def _nav_stats():
    """从导航静态数出页签数与分组数（不启应用，纯正则）。"""
    src = _read(INDEX)
    tabs = set(re.findall(r'data-t="(tab-[a-z0-9-]+)"', src))
    groups = re.findall(r'class="nav-cat"', src)
    return len(tabs), len(groups)


def _tool_count() -> int:
    """**运行时注册表**里的工具数 —— 即 AI 实际能调用的工具数。

    原先统计 `@register_tool(` 装饰器数量，隐含假设"装饰器数 == 运行时数"。
    该假设在「用 registry.register 参数注入注册」的模块上不成立
    （segment_tool.py 就是这样接入以避免循环 import）→ 会少算工具。
    改为直接读注册表，口径与运行时一致。
    """
    import agent_tool_registry as _tr
    reg = _tr.registry
    tools = getattr(reg, '_tools', None) or getattr(reg, 'tools', {}) or {}
    return len(tools)


class TestMirrorSync:
    """github_upload/design 是 design/ 的镜像，漂移了会导致上传到 GitHub 的是旧版。"""

    @pytest.mark.parametrize('name', ['功能介绍_大白话版.md', '简历项目介绍_金融数据分析平台.md'])
    def test_镜像与源文件逐字节一致(self, name):
        src, mir = DESIGN / name, MIRROR / name
        assert src.exists(), f'源文件不存在: {src}'
        assert mir.exists(), f'镜像不存在（新文档需要同步过去）: {mir}'
        h1 = hashlib.md5(src.read_bytes()).hexdigest()
        h2 = hashlib.md5(mir.read_bytes()).hexdigest()
        assert h1 == h2, f'镜像已漂移，请同步 {src.name} → github_upload/design/'

    def test_分册镜像与源文件一致(self):
        """大白话版已拆成「主索引 + 分册」（2026-10-09），分册同样是对外材料，必须同步镜像。"""
        books = sorted((DESIGN / '大白话分册').glob('*.md'))
        assert books, 'design/大白话分册 为空 —— 拆分后的分册可能被误删，主索引里的链接会全部失效'
        for b in books:
            m = MIRROR / '大白话分册' / b.name
            assert m.exists(), f'分册镜像缺失: {b.name}'
            assert hashlib.md5(b.read_bytes()).hexdigest() == \
                hashlib.md5(m.read_bytes()).hexdigest(), f'分册镜像已漂移: {b.name}'


class TestDeclaredNumbersMatchCode:
    """文档声明的规模数字必须等于代码现状。"""

    def test_页签数与分组数与导航一致(self):
        tabs, groups = _nav_stats()
        for doc in (PLAIN, RESUME):
            src = _read(doc)
            assert f'{tabs} 个功能页（{groups} 个分组）' in src, \
                f'{doc.name} 未声明当前口径「{tabs} 个功能页（{groups} 个分组）」（现网页签 {tabs} / 分组 {groups}）'

    def test_工具数与装饰器一致(self):
        n = _tool_count()
        assert f'{n} 个 Agent 工具' in _read(PLAIN), f'大白话版未声明「{n} 个 Agent 工具」'
        assert f'{n} 个领域工具' in _read(RESUME), f'简历版未声明「{n} 个领域工具」'
        assert f'{n} 个工具' in _read(ROOT / '.trae' / 'documents' / '项目简历.md') or \
            f'{n} 个领域工具' in _read(ROOT / '.trae' / 'documents' / '项目简历.md'), \
            f'短版简历未声明「{n} 个（领域）工具」'

    def test_功能模块数与章节数一致(self):
        """大白话版「N 个功能模块」的 N 必须等于正文 `## 功能 N` 的条数。"""
        src = _read(PLAIN)
        declared = int(re.search(r'(\d+) 个功能模块', src).group(1))
        actual = len(re.findall(r'^## 功能 \d+ ·', src, re.M))
        assert declared == actual, f'声明 {declared} 个功能模块，正文只有 {actual} 个「## 功能」小节'

    def test_简历模块数与章节数一致(self):
        src = _read(RESUME)
        actual = len(re.findall(r'^## 模块 \d+ ·', src, re.M))
        assert actual >= 13, f'简历模块数偏少（{actual}），企业深析等模块可能被删'
        # 完整版声明"10 条"成就、精简版"4 条"——只校验它们存在，内容条目数人工维护
        assert '## 一、完整版' in src and '## 二、精简版' in src and '## 三、极简版' in src


class TestNoStaleNumbers:
    """旧数字只能出现在「修订说明 / 事实核查表」的对照位，不得出现在正文战绩里。"""

    # 这些是 09-25 及更早的口径，正文里出现即为过期描述
    STALE = ['32 个 Agent 工具', '32 个领域工具', '39 个功能标签页', '38 个功能标签页']

    @pytest.mark.parametrize('doc', [PLAIN, RESUME])
    def test_正文不含过期工具数页签数(self, doc):
        """只扫**正文行**：修订说明（引用块 `>`）、被划掉的旧行（`~~`）、
        以及明确标注"已过时/旧口径/勿再引用"的对照行允许保留旧数字 —— 那是刻意留的历史证据。"""
        body = []
        for ln in _read(doc).splitlines():
            if ln.lstrip().startswith('>'):
                continue
            if '~~' in ln or any(k in ln for k in ('已过时', '旧口径', '勿再引用', '本版更新', '本版更正')):
                continue
            body.append(ln)
        text = '\n'.join(body)
        hits = [s for s in self.STALE if s in text]
        assert not hits, f'{doc.name} 正文仍有过期表述：{hits}'

    def test_短版简历也是新口径(self):
        src = _read(ROOT / '.trae' / 'documents' / '项目简历.md')
        assert '285 条后端路由' not in src and '909 条自动化测试' not in src, \
            '短版简历还停留在 09-22 口径，需更新'
        assert _tool_count() and 'Fama-French' in src, '短版简历内容缺失'


class TestMarkdownStructure:
    """文档结构守卫：代码围栏必须**独立成行**且成对。

    背景（2026-10-09）：`功能介绍_大白话版.md` 曾把围栏写成
    「下面用三级概括：```」（文字与 ``` 挤在同一行）。按 Markdown 规范，
    围栏必须**独立成行**（最多 3 空格缩进），所以这种写法**不构成围栏起点** →
    后续所有围栏**整体错位一格** → 603 行正文被吞进一个巨大的代码块，
    整篇渲染成一大坨等宽文字。

    ⚠️ 这个缺陷的特征是**完全静默**：不影响任何其它断言，也不报错，
    只能靠肉眼发现。所以专门加断言锁住。
    """

    @pytest.mark.parametrize('doc', [PLAIN, RESUME])
    def test_代码围栏成对(self, doc):
        fences = [l for l in _read(doc).splitlines() if l.strip().startswith('```')]
        assert len(fences) % 2 == 0, \
            f'{doc.name} 代码围栏为奇数（{len(fences)} 个），会有正文被吞进代码块'

    @pytest.mark.parametrize('doc', [PLAIN, RESUME])
    def test_围栏必须独立成行(self, doc):
        bad = [l for l in _read(doc).splitlines()
               if l.rstrip().endswith('```') and not l.lstrip().startswith('```')]
        assert not bad, \
            f'{doc.name} 存在「文字 + ```」同行（围栏必须独立成行）: {bad[:3]}'


class TestRiskPositioning:
    """2026-10-03 为投递「消费金融 / 银行风控」岗位做的定位调整，不得回退。

    背景：项目原来的自我介绍是"A 股量化研究平台"，投消费金融会被误判成"只懂股票"。
    实际上核心资产是**信用评分卡 + 反欺诈规则引擎 + 模型版本治理**（见
    `.trae/documents/中邮消费金融_岗位匹配度_20261003.md`）。这几条断言防止：
    ① 反欺诈模块被删；② Gini 又被漏掉；③ A/B/C 卡分层说明消失；④ 定位退回纯股票口径。
    """

    @pytest.mark.parametrize('doc_name', ['PLAIN', 'RESUME'])
    def test_反欺诈模块已写入文档(self, doc_name):
        src = _read(PLAIN if doc_name == 'PLAIN' else RESUME)
        assert '反欺诈' in src, f'{doc_name} 缺少反欺诈内容'
        assert ('设备聚集' in src) and ('短时高频' in src), f'{doc_name} 缺少反欺诈核心规则'

    @pytest.mark.parametrize('doc_name', ['PLAIN', 'RESUME'])
    def test_声明了_KS_AUC_Gini三指标(self, doc_name):
        src = _read(PLAIN if doc_name == 'PLAIN' else RESUME)
        for kw in ('KS', 'AUC', 'Gini'):
            assert kw in src, f'{doc_name} 未声明 {kw} 指标'

    @pytest.mark.parametrize('doc_name', ['PLAIN', 'RESUME'])
    def test_声明了_A_B_C卡分层(self, doc_name):
        src = _read(PLAIN if doc_name == 'PLAIN' else RESUME)
        for kw in ('A 卡', 'B 卡', 'C 卡'):
            assert kw in src, f'{doc_name} 缺少 {kw} 说明（风控岗必问）'

    def test_定位体现风险建模而非纯股票平台(self):
        assert '风险建模' in _read(PLAIN), '大白话版定位未体现"风险建模"'
        assert '风险建模' in _read(RESUME), '简历版定位未体现"风险建模"'

    def test_模型版本治理被声明(self):
        """`versions`/`rollback`/`compare` 是真实实现（routes/ml_routes.py），必须写进材料。"""
        for doc in (PLAIN, RESUME):
            src = _read(doc)
            assert '回滚' in src and '版本' in src, f'{doc.name} 未声明模型版本治理'
