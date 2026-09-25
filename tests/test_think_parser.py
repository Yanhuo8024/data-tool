import json
import os
import shutil
import subprocess

import pytest


NODE = shutil.which("node")
PARSER = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "js", "think-parser.js",
)


def _split(value):
    if not NODE:
        pytest.skip("node is unavailable")
    script = (
        "const p=require(process.argv[1]);"
        "process.stdout.write(JSON.stringify(p.splitThinkContent(process.argv[2])));"
    )
    result = subprocess.run(
        [NODE, "-e", script, PARSER, value],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def test_complete_think_block_is_separated():
    parts = _split("<think>内部推理</think>\n你好")
    assert parts == {"thinking": "内部推理", "answer": "你好", "inThink": False}


def test_unclosed_streaming_think_block_stays_hidden_from_answer():
    parts = _split("<think>仍在思考")
    assert parts["thinking"] == "仍在思考"
    assert parts["answer"] == ""
    assert parts["inThink"] is True


def test_escaped_and_multiple_think_blocks_are_supported():
    parts = _split("&lt;think&gt;甲&lt;/think&gt;回答<think>乙</think>结束")
    assert parts["thinking"] == "甲乙"
    assert parts["answer"] == "回答结束"


def test_plain_answer_is_unchanged():
    parts = _split("普通回答")
    assert parts == {"thinking": "", "answer": "普通回答", "inThink": False}
