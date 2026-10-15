# -*- coding: utf-8 -*-
"""AI 报告渲染回归（2026-10-07 实战）。

故障：企业深析 / 多Agent 协作报告在聊天区显示为**一大坨无结构文字** ——
      `#` `**` `|` 全部原样可见，换行被压成空格，表格塌成一行，标题与正文同号。

根因（Playwright 实证）：
  1. `run_pipeline_stream` 只发 status/progress/done，**从不发 delta** →
     前端 `accumulated` 为空 → 落到同步渲染分支；
  2. 该分支按 `source` 白名单决定是否走 Markdown，而 `agent_pipeline` / `debate`
     不在名单内 → 整篇报告走 `esc(data.reply)` 纯文本（无 md 类、无换行保留）；
  3. 即便走 Markdown，`formatMD` 也不认 `#`（一级标题）、不认 `---`（分隔线），
     且表格只产出**裸 `<tr>`**（浏览器不渲染成表格）。

本文件锁死：Markdown 渲染行为 + 分派不再按 source 白名单 + 排版样式存在。
"""
import io
import json
import os
import re
import shutil
import subprocess

import pytest

NODE = shutil.which('node')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_JS = os.path.join(ROOT, 'static', 'js', 'core.js')
STYLE_CSS = os.path.join(ROOT, 'static', 'style.css')

HARNESS = """
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
const input = fs.readFileSync(process.argv[3], 'utf8');
globalThis.stripThinking = (t) => t;
eval(code);
process.stdout.write(JSON.stringify({ html: formatMD(input) }));
"""


def _read(path):
    return io.open(path, encoding='utf-8', newline='').read()


def _extract_format_md():
    """从 core.js 抽取 formatMD 函数源码（core.js 是浏览器脚本，不能 require）。

    注意 core.js 是 **CRLF** 行尾，先归一化再按行锚点切，否则 '\n}\n' 静默找不到。
    """
    src = _read(CORE_JS).replace('\r\n', '\n')
    start = src.index('function formatMD(text) {')
    end = src.index('\n}\n', start) + 2
    return src[start:end]


@pytest.fixture(scope='module')
def fmt(tmp_path_factory):
    """返回 (markdown -> html) 的可调用对象；node 不可用则整体跳过。"""
    if not NODE:
        pytest.skip('node is unavailable')
    d = tmp_path_factory.mktemp('md')
    code_p = d / 'fmt.js'
    code_p.write_text(_extract_format_md(), encoding='utf-8')
    harness_p = d / 'harness.js'
    harness_p.write_text(HARNESS, encoding='utf-8')

    def _render(md):
        inp = d / 'in.md'
        # newline='' 禁止 Windows 换行翻译，保证喂进去的就是待测字节
        with io.open(inp, 'w', encoding='utf-8', newline='') as f:
            f.write(md)
        r = subprocess.run([NODE, str(harness_p), str(code_p), str(inp)],
                           check=True, capture_output=True, text=True,
                           encoding='utf-8')
        return json.loads(r.stdout)['html']

    return _render


REPORT = """# 小米集团-W（HK1810）最终研究报告

**首席分析师研判** **数据基准**：现价 24.26 港元

---

## 一、投资结论与核心观点

小米集团在2025年实现营收**4572.87亿元**。

1. 汽车业务跨越生死线
2. 手机高端化兑现价增

| 指标 | 数值 | 同比 |
| :--- | :--- | :--- |
| 营收 | 4572.87亿元 | +24.97% |
| 净利润 | 416.43亿元 | +76.02% |

#### 风控初审：需人工复核

- 缺少免责声明
"""


class TestFormatMD结构渲染:

    def test_一级标题渲染为h2(self, fmt):
        assert '<h2 class="md-h1">一级</h2>' in fmt('# 一级')

    def test_二级标题渲染为h3(self, fmt):
        assert '<h3 class="md-h2">二级</h3>' in fmt('## 二级')

    def test_三四五级标题渲染为h4(self, fmt):
        for md in ('### 三级', '#### 四级', '##### 五级'):
            assert '<h4 class="md-h3">' in fmt(md), md

    def test_标题结尾井号被吃掉(self, fmt):
        assert '<h2 class="md-h1">标题</h2>' in fmt('# 标题 #')

    def test_分隔线渲染为hr(self, fmt):
        out = fmt('上文\n\n---\n\n下文')
        assert '<hr class="md-hr"/>' in out
        assert '---' not in out

    def test_表格被完整包进table(self, fmt):
        """原实现只产裸 <tr>，浏览器不渲染成表格 —— 表格会塌成一行文字。"""
        out = fmt(REPORT)
        assert '<table class="md-table">' in out
        assert '<th>指标</th>' in out
        assert '<td>4572.87亿元</td>' in out

    def test_表格对齐分隔行被丢弃(self, fmt):
        out = fmt('| a | b |\n| :--- | :--- |\n| 1 | 2 |')
        assert ':---' not in out

    def test_无序列表(self, fmt):
        assert '<li class="md-li">条目</li>' in fmt('- 条目')

    def test_有序列表(self, fmt):
        assert 'md-oli' in fmt('1. 第一条')

    def test_粗体(self, fmt):
        assert '<b>重点</b>' in fmt('这是**重点**内容')

    def test_超长不换行表格不逐格break(self, fmt):
        """表格必须整块替换，不能在行尾被 <br/> 打断成一行文字。"""
        out = fmt('| a | b |\n| - | - |\n| 1 | 2 |')
        assert '<br/>' not in out.split('<table')[1].split('</table>')[0]

    def test_CRLF输入与LF等价(self, fmt):
        """报告从 Windows 应用粘贴会带 CRLF；JS 多行 $ 匹配 \\r 之前，
        续行却要匹配 \\n —— 不归一化会让整张表格失效（已实测）。"""
        lf = fmt('# 标题\n\n| a | b |\n| - | - |\n| 1 | 2 |\n')
        crlf = fmt('# 标题\r\n\r\n| a | b |\r\n| - | - |\r\n| 1 | 2 |\r\n')
        assert '<table class="md-table">' in crlf
        assert '<h2 class="md-h1">标题</h2>' in crlf
        assert lf.replace('\r', '') == crlf.replace('\r', '')


class Test安全与不误伤:

    def test_HTML被转义_无XSS(self, fmt):
        out = fmt('<img src=x onerror=alert(1)>')
        assert '&lt;img' in out
        assert '<img' not in out

    def test_代码块保留(self, fmt):
        out = fmt('```python\nprint(1)\n```')
        assert '<pre class="md-pre">' in out and 'print(1)' in out

    def test_行内代码保留(self, fmt):
        assert '<code class="md-code">x</code>' in fmt('用 `x` 表示')

    def test_纯文本原样保留(self, fmt):
        assert '今天天气不错' in fmt('今天天气不错')

    def test_多级列表与正文混排不塌(self, fmt):
        out = fmt('正文段落\n\n- 一\n- 二\n\n结尾段落')
        assert out.count('<li class="md-li">') == 2
        assert '结尾段落' in out


class Test真实报告样本:

    def test_不再残留字面量语法(self, fmt):
        out = fmt(REPORT)
        assert '**' not in out, '粗体语法未渲染，用户会看到字面量星号'
        assert not re.search(r'<br/>#', out), '标题未被识别'

    def test_结构元素齐备(self, fmt):
        out = fmt(REPORT)
        for token in ('md-h1', 'md-h2', 'md-h3', 'md-hr', 'md-table', 'md-li'):
            assert token in out, token


class Test分派不再按source白名单:
    """核心回归：agent_pipeline / debate 报告必须走 Markdown，不能再被 esc()。"""

    def test_渲染分支不再有纯文本兜底(self):
        src = _read(CORE_JS)
        assert 'esc(data.reply)' not in src, (
            'renderChatResult 仍存在 esc(data.reply) 纯文本分支 —— '
            'agent_pipeline / debate 报告会再次塌成无结构长文')

    def test_渲染分支以markdown类输出(self):
        src = _read(CORE_JS)
        i = src.index('// 显示 LLM 回复')
        block = src[i:i + 900]
        assert 'ai-msg assistant md' in block
        assert 'formatMD(data.reply)' in block

    def test_多agent来源判色保留(self):
        src = _read(CORE_JS)
        i = src.index('// 显示 LLM 回复')
        assert "_src.indexOf('agent') === 0" in src[i:i + 900]


class Test排版样式存在:
    """光有 HTML 结构不够 —— 没有 CSS，标题仍与正文同号、表格仍挤成一团。"""

    @pytest.mark.parametrize('sel', [
        '.md-h1', '.md-h2', '.md-h3', '.md-hr',
        '.md-table th', '.md-li', '.md-pre', '.md-code',
    ])
    def test_样式规则存在(self, sel):
        assert sel in _read(STYLE_CSS), f'缺少排版样式: {sel}'