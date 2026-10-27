# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Storage 与 Retrieval 共用的 MemoryUnit 真源复核纯函数。"""

from __future__ import annotations

from datetime import UTC, datetime

from ..time_utils import as_utc
from .filter import FilterExpr
from .hierarchy_query import HierarchyQuery, matches_hierarchy
from .memory import LifecycleState, MemoryUnit
from .memory_filter import matches_memory_unit
from .retrieval import ParsedQuery


def valid_at(unit: MemoryUnit, as_of: datetime) -> bool:
    temporal = unit.temporal
    as_of = as_utc(as_of)
    if temporal.t_valid is not None and as_of < as_utc(temporal.t_valid):
        return False
    if temporal.t_invalid is not None and as_of >= as_utc(temporal.t_invalid):
        return False
    return True


def passes_lifecycle(
    unit: MemoryUnit, as_of: datetime | None, include_archived: bool = False
) -> bool:
    if as_of is None:
        allowed = {LifecycleState.ACTIVE}
        if include_archived:
            allowed.add(LifecycleState.ARCHIVED)
        return unit.lifecycle in allowed and valid_at(unit, datetime.now(UTC))
    return unit.lifecycle != LifecycleState.FORGOTTEN and valid_at(unit, as_of)


def in_event_window(
    unit: MemoryUnit,
    time_from: datetime | None,
    time_to: datetime | None,
    dates: str = "event",
) -> bool:
    """事件窗 ``[time_from, time_to)`` 复核。

    ``dates="event"``（出厂）：只看 ``t_event``，缺失或 naive 时放行。
    ``dates="event_or_message"``：``t_event`` 或 ``t_message``（该记忆被说出的时间）任一落窗
    即命中——抽取常把「过去三个月里…」这类陈述的事件时间记成区间起点，单看 ``t_event`` 会把
    陈述时间在窗内的记忆误判为窗外。此模式下窗不下推索引、本函数是唯一判定点，naive 时间
    按 UTC 归一（as_utc，与索引投影同口径）而不放行；两个时间都未知时放行。
    """
    if time_from is None and time_to is None:
        return True
    if dates == "event_or_message":
        temporal = unit.temporal
        known = [as_utc(t) for t in (temporal.t_event, temporal.t_message) if t is not None]
        if not known:
            return True
        return any(
            (time_from is None or t >= as_utc(time_from))
            and (time_to is None or t < as_utc(time_to))
            for t in known
        )
    event_time = unit.temporal.t_event
    if event_time is None or event_time.tzinfo is None:
        return True
    event_time = as_utc(event_time)
    if time_from is not None and event_time < as_utc(time_from):
        return False
    return time_to is None or event_time < as_utc(time_to)


def matches_retrieval_filters(unit: MemoryUnit, filters: FilterExpr | None) -> bool:
    return matches_memory_unit(unit, filters)


def is_retrieval_candidate(
    unit: MemoryUnit, query: ParsedQuery, *, filters: FilterExpr | None,
) -> bool:
    """复用解析查询复核候选，同时保留调用路径选择的真源过滤表达式。"""
    return (
        passes_lifecycle(unit, query.as_of, query.include_archived)
        and in_event_window(unit, query.time_from, query.time_to, query.time_window_dates)
        and matches_hierarchy(unit, HierarchyQuery.from_query(query))
        and matches_retrieval_filters(unit, filters)
    )
