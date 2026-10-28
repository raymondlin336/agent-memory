# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Rerank server: Cohere-style response, ordering, top_n and HTTP errors (stub scorer, no torch)."""

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "rerank_server.py"
_spec = importlib.util.spec_from_file_location("rerank_server_under_test", _SCRIPT)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)


class _Stub:
    model_name = "stub"

    def logits(self, query, documents):
        return [float(len(d)) - 3.0 for d in documents]  # longer document = more relevant


def test_results_sorted_with_sigmoid_scores():
    out = rs.rerank_response(_Stub(), {"query": "q", "documents": ["a", "abcde", "abc"]})
    assert [r["index"] for r in out["results"]] == [1, 2, 0]
    scores = [r["relevance_score"] for r in out["results"]]
    assert all(0.0 < s < 1.0 for s in scores) and scores == sorted(scores, reverse=True)
    assert scores[1] == pytest.approx(0.5)  # logit 0


def test_top_n_and_text_objects():
    body = {"query": "q", "documents": [{"text": "a"}, {"text": "abcd"}], "top_n": 1}
    out = rs.rerank_response(_Stub(), body)
    assert out == {"results": [{"index": 1, "relevance_score": pytest.approx(0.7311, abs=1e-4)}]}


def test_empty_documents():
    assert rs.rerank_response(_Stub(), {"query": "q", "documents": []}) == {"results": []}


@pytest.mark.parametrize("body", [{"documents": ["a"]}, {"query": "q", "documents": "a"}])
def test_invalid_body_raises(body):
    with pytest.raises(ValueError):
        rs.rerank_response(_Stub(), body)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), rs.make_handler(_Stub()))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def _post(url, payload: bytes):
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_rerank_health_and_errors(server):
    with urllib.request.urlopen(f"{server}/v1", timeout=5) as resp:
        assert json.loads(resp.read()) == {"status": "ok", "model": "stub"}
    status, body = _post(f"{server}/v1/rerank", b'{"query":"q","documents":["a","abc"]}')
    assert status == 200 and [r["index"] for r in body["results"]] == [1, 0]
    assert _post(f"{server}/v1/rerank", b"{bad")[0] == 400
    assert _post(f"{server}/v1/other", b"{}")[0] == 404
