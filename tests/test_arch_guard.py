# -*- coding: utf-8 -*-
"""tests/test_arch_guard.py — 架构守卫的回归入口（R1-R8）

守卫本身必须被测试，否则会退化成"看着在拦、其实没拦"的摆设 —— 本项目已经吃过一次
同类教训：窄签名测试桩被路由的 except 吞掉后从未被调用，而 `seen.get(k) is None`
一类断言仍然通过。所以这里既验证规则能拦，也验证干净输入不误报。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'tools'))

import arch_guard as G  # noqa: E402

BASELINE = G.load_baseline(str(ROOT / 'tools' / 'arch_baseline.json'))


def _errors(root, **kw):
    return [x for x in G.check_tree(root, **kw) if x.severity == 'error']


def test_self_test_passes():
    """内置违规样本：8 条规则必须全部命中，干净样本必须 0 误报"""
    assert G.cmd_self_test() == 0


def test_repo_has_no_blocking_violation():
    """真实仓库在基线下必须 0 阻断级违规（R1-R7 全跑）——这是守卫的实际闸门"""
    errs = _errors(str(ROOT), baseline=BASELINE)
    assert not errs, '架构守卫发现阻断级违规：\n' + '\n'.join(
        f'{x.rule} {x.path}:{x.line} {x.msg}' for x in errs)


def test_baseline_ratchet_blocks_growth(tmp_path):
    """R1 棘轮：超过基线上限即违规；等于上限放行"""
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'sub' / 'a.py').write_text('\n'.join('x = 1' for _ in range(G.MAX_FILE_LINES + 5)),
                                           encoding='utf-8')
    grown = _errors(str(tmp_path), baseline={'file_lines': {'sub/a.py': G.MAX_FILE_LINES + 1}}, rules=['R1'])
    assert grown and grown[0].path == 'sub/a.py'
    assert not _errors(str(tmp_path), baseline={'file_lines': {'sub/a.py': G.MAX_FILE_LINES + 5}}, rules=['R1'])


def test_narrow_test_stub_is_rejected(tmp_path):
    """R6：窄签名桩必须被拦（历史事故：桩从未被调用，断言却假通过）"""
    d = tmp_path / 'tests'
    d.mkdir()
    (d / 'test_x.py').write_text('def fake_chat(a, b):\n    return None\n', encoding='utf-8')
    errs = _errors(str(tmp_path), baseline={}, rules=['R6'])
    assert len(errs) == 1 and 'fake_chat' in errs[0].msg
    (d / 'test_x.py').write_text('def fake_chat(a, b, **kw):\n    return None\n', encoding='utf-8')
    assert not _errors(str(tmp_path), baseline={}, rules=['R6'])


def test_swallow_exception_ratchet(tmp_path):
    """R2：新增"真吞异常"即违规，基线额度内不报"""
    src = 'def %s():\n    try:\n        int("1")\n    except Exception:\n        pass\n'
    (tmp_path / 'sw.py').write_text(src % 'f1', encoding='utf-8')
    assert _errors(str(tmp_path), baseline={}, rules=['R2'])
    base = {'swallow_except': {'sw.py': 1}}
    assert not _errors(str(tmp_path), baseline=base, rules=['R2'])
    (tmp_path / 'sw.py').write_text(src % 'f1' + src % 'f2', encoding='utf-8')
    assert _errors(str(tmp_path), baseline=base, rules=['R2'])


def test_lazy_placeholder_blocked_but_lazy_symbol_allowed(tmp_path):
    """R4：占位符禁止 from-import；非占位惰性符号（能解析到真身）不阻断"""
    (tmp_path / 'm.py').write_text(
        'from core_config import HAS_FIN_MODELS\nfrom core_config import memory_monitor\n', encoding='utf-8')
    errs = _errors(str(tmp_path), baseline={}, placeholders={'HAS_FIN_MODELS'}, rules=['R4'])
    assert len(errs) == 1 and 'HAS_FIN_MODELS' in errs[0].msg


def test_cycle_detection_is_stable(tmp_path):
    """R5：新增环报错，键可入基线后转为 info"""
    (tmp_path / 'a.py').write_text('import b\n', encoding='utf-8')
    (tmp_path / 'b.py').write_text('import a\n', encoding='utf-8')
    errs = _errors(str(tmp_path), baseline={}, rules=['R5'])
    assert len(errs) == 1 and '循环 import' in errs[0].msg
    key = errs[0].msg.split('：')[-1]
    res = G.check_tree(str(tmp_path), baseline={'known_cycles': [key]}, rules=['R5'])
    assert len(res) == 1 and res[0].severity == 'info'


def test_egress_hosts_must_be_registered(tmp_path):
    """R8：出站主机未在 DATA_EGRESS.md 登记即报错；已登记主机与回环地址放行"""
    (tmp_path / 'design').mkdir()
    (tmp_path / 'design' / 'DATA_EGRESS.md').write_text('| `api.registered.example.net` |\n', encoding='utf-8')
    (tmp_path / 'net.py').write_text(
        'import requests\n\n\ndef f():\n'
        '    requests.get("https://unreg-vendor.example.org/x")\n'
        '    requests.get("https://api.registered.example.net/v1")\n'
        '    requests.get("http://127.0.0.1:5000/health")\n', encoding='utf-8')
    errs = _errors(str(tmp_path), baseline={}, rules=['R8'])
    assert len(errs) == 1 and 'unreg-vendor.example.org' in errs[0].msg, errs
    # 把未登记主机写进清单后即放行（清单是白名单，不是"报错就豁免"）
    (tmp_path / 'design' / 'DATA_EGRESS.md').write_text(
        '| `api.registered.example.net` `unreg-vendor.example.org` |\n', encoding='utf-8')
    assert not _errors(str(tmp_path), baseline={}, rules=['R8'])


def test_egress_dynamic_endpoint_is_info_not_error(tmp_path):
    """R8：主机名是动态值（如遥测端点取自环境变量）时只提示不阻断 —— 无法静态登记"""
    (tmp_path / 'design').mkdir()
    (tmp_path / 'design' / 'DATA_EGRESS.md').write_text('# 登记\n', encoding='utf-8')
    (tmp_path / 'trace.py').write_text('import requests\n\n\ndef e(u):\n    return requests.post(u)\n',
                                       encoding='utf-8')
    res = G.check_tree(str(tmp_path), baseline={}, rules=['R8'])
    assert [x.severity for x in res] == ['info'], res
    assert '动态端点' in res[0].msg


def test_guard_scripts_stay_under_line_limit():
    """守卫自身（含拆出的子模块）也必须遵守行数门禁，否则门禁第一天就被自己违反"""
    for name in ('arch_guard.py', 'arch_graph.py', 'arch_egress.py', 'arch_selftest.py'):
        n = len((ROOT / 'tools' / name).read_text(encoding='utf-8').splitlines())
        assert n <= G.MAX_FILE_LINES, f'tools/{name} 共 {n} 行，超过 {G.MAX_FILE_LINES} 行门禁'
