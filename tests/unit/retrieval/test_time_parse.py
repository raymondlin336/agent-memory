"""Rule-based query time-constraint parsing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.retrieval.query_parser_impl.time_parse import parse_time

pytestmark = pytest.mark.unit

NOW = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)


def test_yesterday_returns_day_bounds() -> None:
    lo, hi = parse_time("昨天的会议", now=NOW)

    assert lo == datetime(2026, 6, 15, tzinfo=UTC)
    assert hi == datetime(2026, 6, 16, tzinfo=UTC)


def test_recent_n_days_window_ends_now() -> None:
    lo, hi = parse_time("最近3天", now=NOW)

    assert lo == NOW - timedelta(days=3)
    assert hi == NOW


def test_no_time_expression_returns_none() -> None:
    assert parse_time("just coffee", now=NOW) == (None, None)


# -- 属性问闸门：含时间词但意图是属性查询时，清空 time_from/to 不下推事件窗 ---- #
# 属性问（多大/几岁/爱好/是谁/住址/名字/生日/年龄…）含「今年/昨天」时若按事件时间
# 窗下推，会把 t_event=None 的派生 unit 按缺失字段排他，系统性空召回。
@pytest.mark.parametrize(
    "query",
    [
        "王芳今年多大了",
        "李华今年几岁了",
        "陈静的爱好是什么",
        "王芳是谁",
        "李华的家庭住址在哪里",
        "陈静的生日是哪天",
        "王芳的年龄多少岁",
        "昨天聊了王芳的爱好",  # 含时间词但中心是属性问
    ],
)
def test_attribute_query_with_time_word_returns_none(query: str) -> None:
    """属性问命中关键词即清空时间窗，即便 query 含「今年/昨天」等时间词。"""
    assert parse_time(query, now=NOW) == (None, None)


@pytest.mark.parametrize(
    "query",
    [
        "昨天的会议",  # "会议"不是属性问关键词
        "上周做了什么",
        "最近3天发生了什么",
        "今天的工作记录",
    ],
)
def test_event_query_still_emits_window(query: str) -> None:
    """非属性问的事件问句仍正常解析时间窗。"""
    lo, hi = parse_time(query, now=NOW)
    assert lo is not None and hi is not None, f"事件问句 {query!r} 应有时间窗"


# -- 绝对日期：6月8日 / 2026-06-08 / 2026年6月8日 等应生成当日事件窗 ---------- #
# 缺口A（issue #252）：此前 _rule_based 只覆盖相对时间，绝对日期返回 (None, None)，
# 查询中的日期条件被整体丢弃，时间窗谓词不生成。


def test_absolute_month_day_returns_day_bounds() -> None:
    lo, hi = parse_time("我6月8日的记忆有什么", now=NOW)

    assert lo == datetime(2026, 6, 8, tzinfo=UTC)
    assert hi == datetime(2026, 6, 9, tzinfo=UTC)


def test_absolute_month_day_hao_suffix() -> None:
    lo, _ = parse_time("6月8号我和小王去了湘菜馆", now=NOW)

    assert lo == datetime(2026, 6, 8, tzinfo=UTC)


def test_absolute_date_with_explicit_year() -> None:
    lo, hi = parse_time("2025年6月8日的行程", now=NOW)

    assert lo == datetime(2025, 6, 8, tzinfo=UTC)
    assert hi == datetime(2025, 6, 9, tzinfo=UTC)


def test_absolute_date_iso_format() -> None:
    lo, _ = parse_time("2026-06-08发生了什么", now=NOW)

    assert lo == datetime(2026, 6, 8, tzinfo=UTC)


def test_invalid_absolute_date_returns_none() -> None:
    assert parse_time("我13月40日的记忆", now=NOW) == (None, None)


def test_attribute_query_with_absolute_date_returns_none() -> None:
    """属性问闸门对绝对日期同样生效：生日问句带日期也不下推事件窗。"""
    assert parse_time("王芳6月8日的生日是哪天", now=NOW) == (None, None)


def test_relative_time_still_wins_over_absolute_date() -> None:
    """相对时间词优先级高于绝对日期：命中相对规则即不再取日期窗口。"""
    lo, hi = parse_time("最近3天6月8日", now=NOW)

    assert lo == NOW - timedelta(days=3)
    assert hi == NOW


def test_absolute_date_followed_by_time_digits() -> None:
    """日期后紧跟时间数字（6月8日14点）仍应解析出当日窗口。"""
    lo, hi = parse_time("我6月8日14点的记忆有什么", now=NOW)

    assert lo == datetime(2026, 6, 8, tzinfo=UTC)
    assert hi == datetime(2026, 6, 9, tzinfo=UTC)


def test_year_insertion_form_resolves_explicit_year() -> None:
    """「2025年的6月8日」显式年份不得被 now.year 静默顶替。"""
    lo, hi = parse_time("2025年的6月8日的行程", now=NOW)

    assert lo == datetime(2025, 6, 8, tzinfo=UTC)
    assert hi == datetime(2025, 6, 9, tzinfo=UTC)


# -- 英文规则（english=True）----------------------------------------------- #

EN_NOW = datetime(2023, 3, 25, 18, 4, tzinfo=UTC)  # Saturday


def _utc(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def test_english_rules_off_by_default() -> None:
    assert parse_time("What did I buy 10 days ago?", now=EN_NOW) == (None, None)


@pytest.mark.parametrize(
    ("text", "window"),
    [
        ("What kitchen appliance did I buy 10 days ago?", (_utc(2023, 3, 13), _utc(2023, 3, 18))),
        ("What did I watch a week ago?", (_utc(2023, 3, 17), _utc(2023, 3, 20))),
        ("How many films in the last 3 months?", (_utc(2022, 12, 24), _utc(2023, 3, 26))),
        ("What did I eat yesterday?", (_utc(2023, 3, 24), _utc(2023, 3, 26))),
        ("What did I do the day before yesterday?", (_utc(2023, 3, 23), _utc(2023, 3, 25))),
        ("Which artist did I hear last Friday?", (_utc(2023, 3, 23), _utc(2023, 3, 26))),
        ("Dinner parties in the past month?", (_utc(2023, 2, 1), _utc(2023, 3, 26))),
        ("Events earlier this year?", (_utc(2023, 1, 1), _utc(2023, 3, 26))),
        ("What did I plant in March?", (_utc(2023, 3, 1), _utc(2023, 4, 1))),
        ("Where did I go in December?", (_utc(2022, 12, 1), _utc(2023, 1, 1))),
    ],
)
def test_english_rules_anchor_on_now(text: str, window: tuple[datetime, datetime]) -> None:
    assert parse_time(text, now=EN_NOW, english=True) == window, text


@pytest.mark.parametrize(
    "text",
    [
        "How many days ago did I attend the networking event?",
        "What time do I wake up on Saturday mornings?",
        "Which happened first, the cough or the skin tag removal?",
    ],
)
def test_english_rules_ignore_non_window_questions(text: str) -> None:
    assert parse_time(text, now=EN_NOW, english=True) == (None, None), text


def test_chinese_rules_take_precedence_over_english() -> None:
    assert parse_time("昨天 10 days ago", now=NOW, english=True) == parse_time("昨天", now=NOW)
