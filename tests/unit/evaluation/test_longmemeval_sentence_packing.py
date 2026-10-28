# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""LongMemEval long-turn fallback: sentences are packed into a few chunks, not one group each."""

import re

import pytest

from evaluation.longmemeval.adapter import LongMemEvalDataset

pytestmark = pytest.mark.unit

MAX = 4096


def _groups(turns, max_chars=MAX):
    return LongMemEvalDataset._sentence_fallback_groups(turns, start=0, max_chars=max_chars)


def _long_reply(n_lines=80):
    lines = [f"**Item {i}**: Visit place {i}. It has beaches and food." for i in range(n_lines)]
    return "Here are some ideas.\n\n" + "\n".join(lines)


def test_long_exchange_becomes_few_groups():
    turns = [{"role": "user", "content": "Any travel ideas?"},
             {"role": "assistant", "content": _long_reply(80)}]
    assert len("\n".join(LongMemEvalDataset._turn_content(t) for t in turns)) > MAX
    groups = _groups(turns)
    # one group for the user turn, then the reply in chunks of <= ~half the limit
    assert 2 <= len(groups) <= 4
    assert all(g.granularity == "sentence" for g in groups)


def test_groups_stay_within_limit_and_keep_all_text():
    turns = [{"role": "user", "content": "Q?"}, {"role": "assistant", "content": _long_reply(120)}]
    groups = _groups(turns)
    for g in groups:
        line = LongMemEvalDataset._turn_content(g.turns[0])
        assert len(line) + (len(g.context) + 1 if g.context else 0) <= MAX
    strip = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    assert strip("".join(t["content"] for t in turns)) == strip(
        "".join(g.turns[0]["content"] for g in groups)
    )


def test_later_chunks_carry_preceding_context():
    turns = [{"role": "user", "content": "Which beach should I pick?"},
             {"role": "assistant", "content": _long_reply(120)}]
    groups = _groups(turns)
    assert "Which beach should I pick?" in groups[1].context


def test_pack_sentences_greedy_and_bounded():
    pack = LongMemEvalDataset._pack_sentences
    assert pack(["aaa", "bbb", "ccc"], 7) == ["aaa\nbbb", "ccc"]
    assert pack(["aaaaaaa"], 7) == ["aaaaaaa"]
    assert pack([], 7) == []
