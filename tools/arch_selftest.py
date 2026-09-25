#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tools/arch_selftest.py — 架构守卫的"自证"（守卫自己也要被验证）

为什么单独成文件：守卫的单文件行数门禁是 400 行，而自证需要 7 组违规样本 + 干净样本，
塞在一起会让守卫自身违反它自己的 R1。拆分后两边都达标（与 arch_graph / arch_egress 同理）。

自证要求：内置违规样本必须**逐条规则命中**，干净样本必须 **0 误报**。
理由：本项目真实踩过"守卫看着在拦、其实桩从未被调用"的坑（窄签名测试桩被 except 吞掉、
断言反而假通过），所以规则必须有反向证据。
"""

import os
import tempfile

import arch_guard as g


def _fixtures():
    """{相对路径: 内容} —— 每个样本只针对一条规则，避免相互干扰"""
    filler = 'x = 1\n' * (g.MAX_FILE_LINES + 20)
    return {
        'big.py': filler,                                                   # R0/R1 超长文件
        'swallow.py': 'def f():\n    try:\n        int("1")\n    except Exception:\n        pass\n',
        'base.py': f'try:\n    int("1")\n{g.NEEDLE_BASE_EXC}:\n    pass\n',
        'lazyimp.py': 'from core_config import memory_monitor\n',
        'cyc_a.py': 'import cyc_b\n',
        'cyc_b.py': 'import cyc_a\n',
        'undef.py': 'def f():\n    return never_defined_thing\n',
        # R8：未登记主机必须报错；已登记主机与动态端点必须放行（后两者是"狼来了"的防线）
        'net_out.py': 'import requests\n\n\ndef f():\n'
                      '    return requests.get("https://unreg-vendor.example.org/x")\n',
        'net_ok.py': 'import requests\n\n\ndef g(u=None):\n'
                     '    if u:\n        return requests.get(u)\n'
                     '    return requests.post("https://api.registered.example.net/v1")\n',
        'design/DATA_EGRESS.md': '## 8 出站登记表\n\n| `api.registered.example.net` |\n',
        'tests/test_stub.py': 'def fake_chat(messages):\n    return None\n',
        'clean/sample.py': 'import os\n\n\ndef fine(msgs, **kw):\n    return os.path.join("a", "b")\n',
    }


EXPECT = {'R1': 'big.py', 'R2': 'swallow.py', 'R3': 'base.py', 'R4': 'lazyimp.py', 'R5': 'cyc_a.py',
          'R6': 'tests/test_stub.py', 'R7': 'undef.py', 'R8': 'net_out.py'}


def run_self_test():
    """返回 0（通过）/ 2（有规则未命中或干净样本误报）"""
    with tempfile.TemporaryDirectory() as td:
        for rel, text in _fixtures().items():
            full = os.path.join(td, os.path.normpath(rel))
            os.makedirs(os.path.dirname(full) or td, exist_ok=True)
            with open(full, 'w', encoding='utf-8') as fh:
                fh.write(text)
        res = g.check_tree(td, baseline={}, placeholders={'memory_monitor'})
    got = {}
    for item in res:
        got.setdefault(item.rule, []).append(item.path)
    bad = [f'{r} 未命中 {p}（实得 {got.get(r)}）' for r, p in EXPECT.items() if p not in got.get(r, [])]
    bad += [f'干净样本误报：{x._asdict()}' for x in res
            if x.severity == 'error' and x.path.startswith('clean' + os.sep)]
    print('self-test 命中 →', ' / '.join(f'{k}:{len(v)}' for k, v in sorted(got.items())))
    if bad:
        print('self-test 失败：')
        for b in bad:
            print('  -', b)
        return 2
    print(f'self-test 通过：{len(EXPECT)} 条规则全部命中，干净样本 0 误报')
    return 0


if __name__ == '__main__':
    raise SystemExit(run_self_test())
