# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LongMemEval judge 结论解析：块后结论优先，块后为空时回退到块内 final verdict。"""

import pytest

from evaluation.longmemeval.metrics.llm_judge import _parse_longmemeval_correct

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("<judge_thinking>\nreasons\n</judge_thinking>\nyes", True),
        ("<judge_thinking>\nreasons\n</judge_thinking>\n**No**", False),
        ("<judge_thinking>\nok.\n\nFinal verdict: yes\n</judge_thinking>", True),
        ("<judge_thinking>\nFinal verdict: yes\nwait\nFinal verdict: no\n</judge_thinking>", False),
        ("<judge_thinking>\nFinal verdict: **no**\n</judge_thinking>", False),
        ("<judge_thinking>\nno conclusion here\n</judge_thinking>", None),
        ("<judge_thinking>\nFinal verdict: no\n</judge_thinking>\nyes", True),
        ("yes", True),
    ],
)
def test_parse_longmemeval_verdict(content: str, expected: bool | None) -> None:
    assert _parse_longmemeval_correct(content) is expected, content
