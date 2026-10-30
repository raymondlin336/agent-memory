"""The extractor parser keeps whole replies that contain LaTeX backslashes."""

import pytest

from jiuwen_memory.construction.extractor_impl.llm_extractor import ExtractorImpl

pytestmark = pytest.mark.unit


def _parse(reply: str) -> list[dict]:
    return ExtractorImpl.__new__(ExtractorImpl).parse_llm_response(reply)


def _item(content: str) -> str:
    return f'{{"source_id": "s", "content": "{content}", "tags": ["a", "b"], "confidence": 1.0}}'


def test_invalid_escape_in_first_item_keeps_every_item():
    reply = "[" + _item(r"mass $\alpha$") + "," + _item("plain") + "]"
    items = _parse(reply)
    assert [i["content"] for i in items] == [r"mass $\alpha$", "plain"], "tags list must not win"


def test_invalid_escape_in_last_item_does_not_drop_earlier_items():
    reply = "[" + _item("plain") + "," + _item("plain") + "," + _item(r"$\Delta(G)$") + "]"
    items = _parse(reply)
    assert [i["content"] for i in items] == ["plain", "plain", r"$\Delta(G)$"]


def test_backslash_u_without_hex_is_kept_as_text():
    assert _parse("[" + _item(r"\usepackage") + "]")[0]["content"] == r"\usepackage"


def test_valid_escapes_are_unchanged():
    reply = "[" + _item(r'line\nnext \"q\" back\\slash é') + "]"
    assert _parse(reply)[0]["content"] == 'line\nnext "q" back\\slash é'


def test_escaped_backslash_before_letter_stays_one_backslash():
    assert _parse("[" + _item(r"$\\alpha$") + "]")[0]["content"] == r"$\alpha$"


def test_fenced_reply_is_still_parsed():
    reply = "```json\n[" + _item(r"$\alpha$") + "]\n```"
    assert _parse(reply)[0]["content"] == r"$\alpha$"
