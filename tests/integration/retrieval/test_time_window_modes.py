"""英文时间窗按 event / event_or_message 判定时间的端到端行为。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jiuwen_memory.common.errors import ValidationError
from jiuwen_memory.retrieval.fuser_impl.rrf_fuser import RRFFuser
from jiuwen_memory.retrieval.query_parser_impl.simple_query_parser import SimpleQueryParser
from jiuwen_memory.retrieval.retriever_impl.pipeline_retriever import PipelineRetriever
from jiuwen_memory.retrieval.types import RetrievalQuery
from tests.conftest import DEFAULT_SCOPE, index_unit, make_unit, make_world

pytestmark = pytest.mark.integration


def _days_ago(n: int) -> datetime:
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=n)


def _world_with_purchases():
    # 「10 days ago」的窗是 [今天-12, 今天-7)。
    world = make_world()
    index_unit(world, make_unit("smoker", "user bought a smoker", t_event=_days_ago(10)))
    index_unit(world, make_unit("boots", "user bought boots", t_event=_days_ago(43)))
    index_unit(
        world,
        make_unit(
            "faucet",
            "user bought a faucet over the past weeks",
            t_event=_days_ago(33),
            t_message=_days_ago(11) + timedelta(hours=9),
        ),
    )
    return world


def _retriever(world, **kwargs) -> PipelineRetriever:
    parser = SimpleQueryParser(world.tokenizer, world.embedder, time_rules_en=True)
    return PipelineRetriever(
        parser,
        RRFFuser(),
        world.discloser,
        world.unit_reader,
        None,
        domain_store=world.retriever.storage,
        **kwargs,
    )


def _ids(retriever: PipelineRetriever) -> list[str]:
    query = RetrievalQuery(text="what did user buy 10 days ago", top_k=5)
    return [item.unit_id for item in retriever.retrieve(DEFAULT_SCOPE, query).items]


def test_filter_mode_drops_out_of_window() -> None:
    ids = _ids(_retriever(_world_with_purchases()))

    assert ids == ["smoker"], "filter + event：只留 t_event 落窗的记忆"


def test_filter_mode_event_or_message_keeps_said_in_window() -> None:
    ids = _ids(_retriever(_world_with_purchases(), time_window_dates="event_or_message"))

    assert sorted(ids) == ["faucet", "smoker"], "陈述时间落窗的记忆也命中"


def test_invalid_time_window_dates_rejected() -> None:
    with pytest.raises(ValidationError):
        _retriever(make_world(), time_window_dates="message")
