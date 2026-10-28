# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""OpenAIEmbedder vector cache and VectorDedup.prefetch: fewer API calls, identical vectors."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from jiuwen_memory.common.embedder.embedder_impl.openai_embedder import OpenAIEmbedder
from jiuwen_memory.construction.dedup_impl.vector_dedup import VectorDedup

pytestmark = pytest.mark.unit


def _vec(text: str, dim: int = 4) -> list[float]:
    return [float(len(text)), float(ord(text[0])), 0.5, -1.0][:dim]


def _embedder(**kwargs) -> tuple[OpenAIEmbedder, list[list[str]]]:
    calls: list[list[str]] = []

    def create(model, input, **_):  # noqa: A002 - mirrors the SDK keyword
        calls.append(list(input))
        return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=_vec(t))
                                     for i, t in enumerate(input)])

    emb = OpenAIEmbedder(api_key="k", model_name="text-embedding-v4", dimension=4, **kwargs)
    client = MagicMock()
    client.embeddings.create = MagicMock(side_effect=create)
    emb._client = client
    emb._client_fingerprint = ("k", None, False, None)
    return emb, calls


def test_repeated_texts_are_not_requested_again():
    emb, calls = _embedder()
    first = emb.embed(["alpha", "beta"])
    second = emb.embed(["beta", "alpha"])
    assert calls == [["alpha", "beta"]]
    assert second == [first[1], first[0]]


def test_only_misses_are_requested_once_and_order_kept():
    emb, calls = _embedder()
    emb.embed(["a1"])
    out = emb.embed(["b22", "a1", "b22", "c333"])
    assert calls == [["a1"], ["b22", "c333"]]
    assert out == [_vec("b22"), _vec("a1"), _vec("b22"), _vec("c333")]


def test_returned_vectors_are_copies():
    emb, _ = _embedder()
    emb.embed(["x"])[0][0] = 999.0
    assert emb.embed(["x"])[0] == _vec("x")


def test_cache_size_zero_disables_cache():
    emb, calls = _embedder(cache_size=0)
    emb.embed(["same"])
    emb.embed(["same"])
    assert calls == [["same"], ["same"]]


def test_lru_evicts_oldest():
    emb, calls = _embedder(cache_size=2)
    emb.embed(["one", "two", "three"])  # "one" evicted
    emb.embed(["three", "one"])
    assert calls == [["one", "two", "three"], ["one"]]


def test_key_includes_dimension_and_model():
    emb, calls = _embedder()
    emb.embed(["t"])
    emb._dimension = 8
    emb.embed(["t"])
    emb._fallback_model = "text-embedding-v3"
    emb.embed(["t"])
    assert len(calls) == 3


def test_vector_dedup_prefetch_batches_candidates():
    embedder = MagicMock()
    dedup = VectorDedup.__new__(VectorDedup)
    dedup._embedder = embedder
    units = [SimpleNamespace(content=c) for c in ("fact a", "fact b", "fact a", "")]
    dedup.prefetch(units)
    embedder.embed.assert_called_once_with(["fact a", "fact b"])


def test_vector_dedup_prefetch_swallows_errors():
    embedder = MagicMock()
    embedder.embed.side_effect = RuntimeError("upstream down")
    dedup = VectorDedup.__new__(VectorDedup)
    dedup._embedder = embedder
    dedup.prefetch([SimpleNamespace(content="x"), SimpleNamespace(content="y")])


def test_vector_dedup_prefetch_skips_embedders_without_cache():
    embedder = MagicMock()
    embedder.caches_vectors = False
    dedup = VectorDedup.__new__(VectorDedup)
    dedup._embedder = embedder
    dedup.prefetch([SimpleNamespace(content="x"), SimpleNamespace(content="y")])
    embedder.embed.assert_not_called()


def test_caches_vectors_flag():
    assert _embedder()[0].caches_vectors is True
    assert _embedder(cache_size=0)[0].caches_vectors is False
