# -*- coding: utf-8 -*-
"""web_search.py 单测（格式与失败分支；真实搜索需网络，做标记跳过）"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web_search import format_results, web_search, _clean, _decode_ddg_url


def test_clean():
    # <a> 标签变为空格，&amp; 反转义为 &
    assert _clean('<a href="#">A&amp;B</a>') == 'A&B'


def test_decode_ddg_url():
    u = _decode_ddg_url('https://duckduckgo.com/l/?uddg=' + 'https%3A%2F%2Fexample.com%2Fx')
    assert u == 'https://example.com/x'
    assert _decode_ddg_url('https://example.com') == 'https://example.com'


def test_empty_query():
    r = web_search('   ')
    assert r['ok'] is False


@pytest.mark.network
def test_live_search():
    """真实搜索（有网时跑）"""
    r = web_search('上证指数', top_k=3)
    assert r['ok'] is True
    assert len(r['results']) >= 1
    assert r['results'][0]['title']


def test_format_empty():
    s = format_results('   ')
    assert s  # 返回错误说明而非异常
