# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""查询时间约束解析：自然语言 → 事件时间窗 ``(time_from, time_to)``（event-time）。

**规则版**：覆盖中文常见相对时间（今天/昨天/前天/本周/上周/本月/上个月/今年/
去年/最近 N 天/过去 N 周…）与绝对日期（2026年6月8日 / 2026-06-08 / 6月8日/号，
无年份按当前年补齐，规则与写入侧共用 ：func:`common.time_utils.first_absolute_date`）。
命中即返回闭/半开区间的起止 ``datetime``，未命中返回 ``(None, None)``。

**属性问闸门**：属性问（多大/几岁/爱好/是谁/住址/名字/生日/年龄…）即使含时间词
（如「王芳今年多大了」「李华上周爱好是什么」）也不应下推事件窗——这是属性查询
而非事件时间检索，误下推会让无事件时间（``t_event=None``）的派生 unit 在索引里
被 ``t_event GTE/LT`` 按缺失字段排他。命中属性问关键词即清空 ``time_from/to``。

**英文规则**（``english=True`` 时启用，中文规则未命中后再试）：``N days/weeks/months ago``
（以该点为中心给容差窗）、``last/past N days|weeks|months|years``、``yesterday``、
``last <weekday>``、``last/past week|month|year``、``this week|month|year``、``in <Month> [YYYY]``。
「习惯类」表述（``on Saturday mornings``）不视为时间约束。

**LLM 钩子**：:func:`parse_time` 接受可选 ``llm``——规则未命中时可委托 LLM 解析
（如解析「去年双十一前后」这类规则难穷举的表述）。默认不启用（传 ``None``），由
调用方决定是否注入，避免规则可控性被弱模型噪声污染。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from jiuwen_memory.common.time_utils import first_absolute_date

TimeWindow = tuple[datetime | None, datetime | None]

# 属性问强信号关键词：命中即判为属性问，清空时间窗。
# 必须避开「会议/工作」等中性名词——误判会让真事件问句漏召回；
# 这些词是「询问对象属性」的高置信信号（多大/几岁 = 年龄、爱好 = 偏好、
# 是谁/叫什么/名字 = 身份、住址/地址 = 位置、生日 = 纪念日）。
_ATTRIBUTE_QUERY_PATTERN = re.compile(
    r"多大|几岁|多少岁|多大了|爱好|是谁|叫什么|叫什么名字|名字|"
    r"住址|住哪|住在哪|地址|生日|年龄|岁数"
)


def _day_bounds(d: datetime) -> TimeWindow:
    start = d.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _month_start(d: datetime) -> datetime:
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _rule_based(text: str, now: datetime) -> TimeWindow:
    t = text.strip()

    # 最近/过去 N 天｜周（到 now）
    m = re.search(r"(最近|过去|近)\s*(\d+)\s*(天|日|周|星期)", t)
    if m:
        n = int(m.group(2))
        delta = timedelta(weeks=n) if m.group(3) in ("周", "星期") else timedelta(days=n)
        return now - delta, now

    if "前天" in t:
        return _day_bounds(now - timedelta(days=2))
    if "昨天" in t or "昨日" in t:
        return _day_bounds(now - timedelta(days=1))
    if "今天" in t or "今日" in t:
        return _day_bounds(now)

    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if "上周" in t or "上星期" in t:
        return week_start - timedelta(days=7), week_start
    if "本周" in t or "这周" in t or "这星期" in t:
        return week_start, now

    month_start = _month_start(now)
    if "上个月" in t or "上月" in t:
        prev_end = month_start
        prev_start = _month_start(month_start - timedelta(days=1))
        return prev_start, prev_end
    if "本月" in t or "这个月" in t:
        return month_start, now

    year_start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    if "去年" in t:
        return year_start.replace(year=year_start.year - 1), year_start
    if "今年" in t:
        return year_start, now

    # 绝对日期（2026年6月8日 / 2026-06-08 / 6月8日/号）：生成当日事件窗。放在相对
    # 规则之后——相对词（今天/最近3天）是更强的意图信号；与写入侧共用
    # common.time_utils 的同一份规则，无年份日期两侧都按各自 now 的年份补齐。
    date_hit = first_absolute_date(t, now)
    if date_hit is not None:
        return _day_bounds(date_hit)

    return None, None


_EN_NUM = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "couple of": 2, "few": 3,
}
_EN_N = r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|couple of|few)"
_EN_UNIT_DAYS = {"day": 1, "week": 7, "month": 30, "year": 365}
# 「N 单位前」很少精确到天：以该点为中心给容差（不超过跨度的 1/4，至少 1 天）。
_EN_TOLERANCE_DAYS = {"day": 2, "week": 4, "month": 15, "year": 60}
_EN_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_EN_MONTHS = [
    "january", "february", "march", "april", "may", "june", "july", "august", "september",
    "october", "november", "december",
]
_DAY = timedelta(days=1)


def _en_num(token: str) -> int:
    return int(token) if token.isdigit() else _EN_NUM[token]


def _rule_based_en(text: str, now: datetime) -> TimeWindow:
    t = " ".join(text.lower().split())
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)

    m = re.search(rf"\b{_EN_N}\s+(day|week|month|year)s?\s+ago\b", t)
    if m:
        n, unit = _en_num(m.group(1)), m.group(2)
        center = today - timedelta(days=n * _EN_UNIT_DAYS[unit])
        tol = timedelta(days=min(_EN_TOLERANCE_DAYS[unit], max(1, n * _EN_UNIT_DAYS[unit] // 4)))
        return center - tol, center + tol + _DAY

    m = re.search(rf"\b(?:last|past|previous)\s+{_EN_N}\s+(day|week|month|year)s?\b", t)
    if m:
        n, unit = _en_num(m.group(1)), m.group(2)
        return today - timedelta(days=n * _EN_UNIT_DAYS[unit]) - _DAY, today + _DAY

    if re.search(r"\bday before yesterday\b", t):
        return today - 2 * _DAY, today
    if re.search(r"\byesterday\b", t):
        return today - _DAY, today + _DAY

    m = re.search(r"\b(?:last|this past|past)\s+(" + "|".join(_EN_WEEKDAYS) + r")\b", t)
    if m:
        back = (today.weekday() - _EN_WEEKDAYS.index(m.group(1))) % 7 or 7
        day = today - timedelta(days=back)
        return day - _DAY, day + 2 * _DAY

    m = re.search(r"\b(?:last|past|previous)\s+(week|month|year)\b", t)
    if m:
        unit = m.group(1)
        if unit == "week":
            return today - timedelta(days=14), today + _DAY
        if unit == "month":
            prev_start = (today.replace(day=1) - _DAY).replace(day=1)
            return min(prev_start, today - timedelta(days=31)), today + _DAY
        return today.replace(year=today.year - 1, month=1, day=1), today + _DAY

    m = re.search(r"\b(?:this|earlier this)\s+(week|month|year)\b", t)
    if m:
        start = {
            "week": today - timedelta(days=today.weekday()),
            "month": today.replace(day=1),
            "year": today.replace(month=1, day=1),
        }[m.group(1)]
        return start, today + _DAY

    m = re.search(r"\b(?:in|during)\s+(" + "|".join(_EN_MONTHS) + r")(?:\s+(\d{4}))?\b", t)
    if m:
        month = _EN_MONTHS.index(m.group(1)) + 1
        year = int(m.group(2)) if m.group(2) else (
            today.year if month <= today.month else today.year - 1
        )
        start = today.replace(year=year, month=month, day=1)
        return start, (start + timedelta(days=32)).replace(day=1)

    return None, None


def parse_time(
    text: str,
    now: datetime | None = None,
    llm: object = None,  # LLM 钩子：注入即在规则未命中时委托其解析（默认不启用）
    *,
    english: bool = False,
) -> TimeWindow:
    """解析 ``text`` 中的事件时间约束，返回 ``(time_from, time_to)``。

    属性问（命中 ``_ATTRIBUTE_QUERY_PATTERN``）直接返回 ``(None, None)``——
    属性查询不应被误当事件时间检索下推，否则 ``t_event=None`` 的派生 unit
    会被索引按缺失字段排他，系统性空召回。
    """
    now = now or datetime.now(UTC)
    if _ATTRIBUTE_QUERY_PATTERN.search(text):
        return None, None
    win = _rule_based(text, now)
    if win != (None, None):
        return win
    if english:
        win = _rule_based_en(text, now)
        if win != (None, None):
            return win
    # 规则未命中：此处可接 LLM 解析（钩子）。默认 llm=None，不启用。
    # if llm is not None: return _llm_based(llm, text, now)
    return None, None
