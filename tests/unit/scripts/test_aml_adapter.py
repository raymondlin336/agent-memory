# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AML 适配器：协议形状、幂等断点续写、鉴权、与真实内存引擎的授权链路。"""

import importlib.util
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "aml_adapter.py"


@pytest.fixture(name="aml")
def _aml(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("aml_adapter_under_test", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass 解析注解需要模块已注册
    spec.loader.exec_module(module)
    module._ledger = module.Ledger(str(tmp_path / "ledger.db"))
    monkeypatch.setattr(module, "_security", lambda: "ctx")
    yield module
    module._ledger.close()
    sys.modules.pop(spec.name, None)


class FakeApi:
    def __init__(self):
        self.adds: list[dict] = []
        self.fail_on_add: int | None = None
        self.persist_then_fail_on: int | None = None
        self.list_error: Exception | None = None
        self.items: list = []
        self.units: dict = {}
        self.search_kwargs: dict = {}

    def add(self, content, scope, **kwargs):
        if self.fail_on_add == len(self.adds):
            self.fail_on_add = None
            raise RuntimeError("llm down")
        self.adds.append({"content": content, "scope": scope, **kwargs})
        if self.persist_then_fail_on == len(self.adds) - 1:
            self.persist_then_fail_on = None
            raise RuntimeError("crashed after the engine persisted")
        return []

    def list(self, scope, *, filters=None, **kwargs):
        if self.list_error:
            raise self.list_error
        wanted = filters[0].value
        hits = [
            a for a in self.adds
            if a["system_metadata"]["seed_key"] == wanted and a["scope"].user == scope.user
        ]
        return SimpleNamespace(items=hits[:1])

    def search(self, query, context, **kwargs):
        self.search_kwargs = {"query": query, "scope": context.scope, **kwargs}
        return SimpleNamespace(items=self.items)

    def inspect(self, ids, scope, **kwargs):
        return [self.units[i] for i in ids if i in self.units]


def _add_body(**overrides):
    body = {
        "request_id": "req-1",
        "user_id": "u1",
        "session_id": "s1",
        "messages": [
            {"role": "user", "content": "I adopted a dog named Rex", "timestamp": 1700000000000},
            {"role": "assistant", "content": "Congrats!", "timestamp": 1700000060000},
            {"role": "user", "content": "He is a beagle", "timestamp": 1700000120000},
        ],
    }
    body.update(overrides)
    return body


def _item(uid, content="memo", score=0.5):
    return SimpleNamespace(unit_id=uid, content=content, score=score)


def _unit(uid, t_message=None, t_event=None):
    return SimpleNamespace(id=uid, temporal=SimpleNamespace(t_message=t_message, t_event=t_event))


# ---------------------------------------------------------------- Add ---


def test_add_echoes_fields_and_writes_dialogue_turns(aml):
    aml.api = FakeApi()
    out = aml.handle_add(_add_body())
    assert out == {"success": True, "request_id": "req-1", "user_id": "u1", "session_id": "s1"}, (
        "response must echo ids"
    )
    adds = aml.api.adds
    assert [a["content"] for a in adds] == [
        "[user]: I adopted a dog named Rex\n[assistant]: Congrats!",
        "[user]: He is a beagle",
    ], "user+assistant pair must be merged into one dialogue turn, orphan kept single"
    assert adds[0]["scope"].user == "u-u1" and adds[0]["scope"].org == aml.ORG
    assert adds[0]["tags"] == ["s1"]
    meta = adds[0]["system_metadata"]
    assert meta["infer"] == "true" and meta["retain_source"] == "false"
    assert meta["session"] == "s1" and meta["observation_date"] == "2023-11-14"
    assert adds[0]["occurred_at"] == datetime(2023, 11, 14, 22, 13, 20), (
        "timestamp (ms) must become naive-UTC occurred_at of the group's first message"
    )
    assert adds[1]["occurred_at"] == datetime(2023, 11, 14, 22, 15, 20)


def test_add_without_timestamps_uses_engine_default(aml):
    aml.api = FakeApi()
    body = _add_body(messages=[{"role": "user", "content": "hello"}])
    aml.handle_add(body)
    assert aml.api.adds[0]["occurred_at"] is None
    assert aml.api.adds[0]["system_metadata"]["observation_date"] == ""


def test_add_missing_timestamp_borrows_neighbour(aml):
    aml.api = FakeApi()
    body = _add_body(
        messages=[
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b", "timestamp": 1700000000000},
        ]
    )
    aml.handle_add(body)
    assert aml.api.adds[0]["occurred_at"] == datetime(2023, 11, 14, 22, 13, 20)


def test_add_blank_messages_are_skipped_but_succeed(aml):
    aml.api = FakeApi()
    out = aml.handle_add(_add_body(messages=[{"role": "user", "content": "   "}]))
    assert out["success"] is True and aml.api.adds == []


def test_add_retry_after_completion_does_not_rewrite(aml):
    aml.api = FakeApi()
    aml.handle_add(_add_body())
    first = len(aml.api.adds)
    aml.handle_add(_add_body())
    assert len(aml.api.adds) == first, "completed request_id must replay 200 without writing"


def test_add_retry_resumes_from_failed_group(aml):
    aml.api = FakeApi()
    aml.api.fail_on_add = 1  # 第二个 group 失败
    with pytest.raises(RuntimeError):
        aml.handle_add(_add_body())
    assert len(aml.api.adds) == 1
    aml.handle_add(_add_body())
    assert [a["content"] for a in aml.api.adds] == [
        "[user]: I adopted a dog named Rex\n[assistant]: Congrats!",
        "[user]: He is a beagle",
    ], "retry must continue after the persisted group, not duplicate it"


def test_add_resume_survives_ledger_reopen(aml, tmp_path):
    aml.api = FakeApi()
    aml.api.fail_on_add = 1
    with pytest.raises(RuntimeError):
        aml.handle_add(_add_body())
    aml._ledger.close()
    aml._ledger = aml.Ledger(str(tmp_path / "ledger.db"))  # 模拟进程重启
    aml.handle_add(_add_body())
    assert len(aml.api.adds) == 2


def test_add_same_request_id_new_payload_is_treated_as_new(aml):
    aml.api = FakeApi()
    aml.handle_add(_add_body())
    aml.handle_add(_add_body(messages=[{"role": "user", "content": "different"}]))
    assert aml.api.adds[-1]["content"] == "[user]: different"


def test_add_request_ids_are_per_user(aml):
    aml.api = FakeApi()
    aml.handle_add(_add_body())
    aml.handle_add(_add_body(user_id="u2"))
    assert {a["scope"].user for a in aml.api.adds} == {"u-u1", "u-u2"}


def test_add_inflight_duplicate_gets_409(aml):
    aml.api = FakeApi()
    aml._inflight.add(("u1", "req-1"))
    try:
        with pytest.raises(aml.HttpError) as err:
            aml.handle_add(_add_body())
    finally:
        aml._inflight.discard(("u1", "req-1"))
    assert err.value.status == 409 and err.value.retry_after


@pytest.mark.parametrize(
    "body",
    [
        {},
        _add_body(request_id=""),
        _add_body(user_id=None),
        _add_body(session_id=5),
        _add_body(messages=[]),
        _add_body(messages="x"),
        _add_body(messages=["x"]),
    ],
)
def test_add_rejects_bad_requests_with_422(aml, body):
    aml.api = FakeApi()
    with pytest.raises(aml.HttpError) as err:
        aml.handle_add(body)
    assert err.value.status == 422, "contract errors must be non-retryable 4xx"


def test_long_turn_falls_back_to_sentences_with_context(aml):
    aml.api = FakeApi()
    sentence = "This is a fairly long sentence about my trip. "
    text = sentence * 200  # 远超 4096 字符
    aml.handle_add(_add_body(messages=[{"role": "user", "content": text, "timestamp": 1}]))
    assert len(aml.api.adds) > 1
    assert all(len(a["content"]) <= aml.DIALOGUE_TURN_MAX_CHARS for a in aml.api.adds)
    assert any(aml.LONG_TURN_CONTEXT_KEY in a["system_metadata"] for a in aml.api.adds[1:])


# ------------------------------------------------------------- Search ---


def test_search_shape_cap_and_filters(aml):
    api = FakeApi()
    api.items = [
        _item("a", "first", 0.9),
        _item("a", "dup", 0.8),
        _item("", "no id"),
        _item("b", "   "),
        _item("c", "third", float("nan")),
        _item("d", "fourth", 0.1),
    ]
    api.units = {"a": _unit("a", t_message=datetime(2023, 5, 7, 10, 0)), "c": _unit("c")}
    aml.api = api
    out = aml.handle_search({"query": "q", "user_id": "u1", "top_k": 2})
    assert [d["id"] for d in out["data"]] == ["a", "c"], "dedupe, drop empties, cap at top_k"
    assert out["data"][0]["content"] == "[2023-05-07] first"
    assert out["data"][0]["created_at"] == "2023-05-07T10:00:00Z"
    assert "created_at" not in out["data"][1] and "score" not in out["data"][1]
    assert api.search_kwargs["top_k"] == 2 and api.search_kwargs["scope"].user == "u-u1"


def test_search_empty_and_defaults(aml):
    aml.api = FakeApi()
    assert aml.handle_search({"query": "q", "user_id": "u"}) == {"data": []}
    assert aml.api.search_kwargs["top_k"] == aml.DEFAULT_TOP_K


@pytest.mark.parametrize("top_k", [0, -1, "5", 1.5, True])
def test_search_rejects_bad_top_k(aml, top_k):
    aml.api = FakeApi()
    with pytest.raises(aml.HttpError) as err:
        aml.handle_search({"query": "q", "user_id": "u", "top_k": top_k})
    assert err.value.status == 422


def test_search_options_only_appended_when_enabled(aml, monkeypatch):
    aml.api = FakeApi()
    aml.handle_search({"query": "q", "user_id": "u", "options": ["A", "B"]})
    assert aml.api.search_kwargs["query"] == "q"
    monkeypatch.setattr(aml, "OPTIONS_IN_QUERY", True)
    aml.handle_search({"query": "q", "user_id": "u", "options": ["A", "B"]})
    assert aml.api.search_kwargs["query"] == "q\nA\nB"


# --------------------------------------------------------------- Auth ---


@pytest.mark.parametrize(
    "headers,ok",
    [
        ({"Authorization": "Bearer k"}, True),
        ({"Authorization": "Token k"}, True),
        ({"X-Api-Key": "k"}, True),
        ({"Authorization": "Bearer wrong", "X-Api-Key": "k"}, True),
        ({"Authorization": "Bearer wrong"}, False),
        ({}, False),
    ],
)
def test_authorization_variants(aml, monkeypatch, headers, ok):
    monkeypatch.setattr(aml, "API_KEY", "k")
    assert aml._authorized(headers) is ok


_STRONG = "x" * 32


@pytest.mark.parametrize(
    ("host", "key", "ok"),
    [
        ("127.0.0.1", "", True),
        ("localhost", "local-dev-key", True),
        ("::1", "k", True),
        ("0.0.0.0", "", False),
        ("0.0.0.0", "local-dev-key", False),
        ("0.0.0.0", "x" * 31, False),
        ("0.0.0.0", _STRONG, True),
        ("10.0.0.5", _STRONG, True),
        ("example.org", "short", False),
    ],
)
def test_public_bind_requires_strong_key(aml, host, key, ok):
    assert (aml.public_bind_problem(host, key) is None) is ok


def test_classify_maps_engine_errors(aml):
    assert aml._classify(aml.ValidationError("bad")).status == 422
    assert aml._classify(aml.RateLimitedError("slow")).status == 429
    assert aml._classify(aml.PermissionDeniedError("nope")).status == 500
    assert aml._classify(RuntimeError("boom")).status == 503


# ------------------------------------------------- HTTP 端到端（假引擎）---


@pytest.fixture(name="server")
def _server(aml, monkeypatch):
    monkeypatch.setattr(aml, "API_KEY", "k")
    aml.api = FakeApi()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), aml.Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _call(url, body=None, headers=None, method=None):
    req = urllib.request.Request(
        url,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_http_health_is_unauthenticated(server):
    assert _call(server + "/health")[0] == 200
    assert _call(server + "/")[0] == 200


def test_http_requires_auth_and_routes(server):
    assert _call(server + "/add", _add_body())[0] == 401
    assert _call(server + "/nope", {}, {"X-Api-Key": "k"})[0] == 404


def test_http_add_then_search(server):
    status, body = _call(server + "/add", _add_body(), {"Authorization": "Bearer k"})
    assert status == 200 and body["request_id"] == "req-1"
    status, body = _call(
        server + "/search", {"query": "dog", "user_id": "u1", "top_k": 100}, {"X-Api-Key": "k"}
    )
    assert status == 200 and body == {"data": []}


def test_http_bad_json_is_400_and_engine_failure_is_503(server, aml):
    url, auth = server + "/add", {"X-Api-Key": "k"}
    req = urllib.request.Request(url, data=b"{not json", headers=auth, method="POST")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=10)
    assert err.value.code == 400
    aml.api.fail_on_add = 0
    assert _call(url, _add_body(), auth)[0] == 503, "engine failures must be retryable (5xx)"


# ------------------------------------- 真实内存引擎：授权链路不被拒绝 ---


def test_real_in_memory_engine_accepts_adapter_security(aml, monkeypatch):
    monkeypatch.undo()  # 恢复真实的 _security
    from jiuwen_memory.api import assemble_runtime, build_dev_authenticator

    runtime = assemble_runtime()
    try:
        aml.api = runtime.api
        aml._authenticator = build_dev_authenticator()
        aml.selfcheck()
        out = aml.handle_add(_add_body())
        assert out["success"] is True
        aml.handle_search({"query": "dog", "user_id": "u1", "top_k": 5})
    finally:
        runtime.close()


# ----------------------------------------------------------------- 配置 ---


def test_aml_config_pins_benchmark_retrieval_and_immediate_visibility():
    from evaluation.shared.config_loader import load_layer

    cfg = load_layer(str(_SCRIPT.parents[1] / "deploy" / "aml" / "config.yml"))["memory_api"]
    assert cfg["globals"]["rerank_enabled"] is True and cfg["globals"]["embedder_dim"] == 1024
    retriever = cfg["retriever"]["default"]["params"]
    assert retriever["recall_max"] == 300 and retriever["rerank_max"] == 300
    assert retriever["rerank_fused_weight"] == 0.0
    assert cfg["reranker"]["default"]["params"]["reranker_dialect"] == "cohere"
    for name, store in cfg["fulltext_store"].items():
        assert store["params"]["refresh"] == "wait_for", f"{name}: ES must refresh on write"
    for store in cfg["vector_store"].values():
        assert store["params"]["dim"] == 1024


# ------------------------------------------------ 崩溃恢复 / 截止 / 用户 ID ---


def test_crash_between_engine_write_and_ledger_does_not_duplicate(aml):
    aml.api = FakeApi()
    aml.api.persist_then_fail_on = 0  # 第一个 group 已落库但没来得及记账
    with pytest.raises(RuntimeError):
        aml.handle_add(_add_body())
    aml.handle_add(_add_body())
    assert [a["system_metadata"]["seed_key"] for a in aml.api.adds] == ["req-1#0", "req-1#1"], (
        "the already-persisted group must be detected via seed_key, not written twice"
    )


def test_probe_failure_prefers_duplicate_over_loss(aml):
    aml.api = FakeApi()
    aml.api.persist_then_fail_on = 0
    with pytest.raises(RuntimeError):
        aml.handle_add(_add_body())
    aml.api.list_error = RuntimeError("index down")
    aml.handle_add(_add_body())
    assert [a["system_metadata"]["seed_key"] for a in aml.api.adds] == [
        "req-1#0", "req-1#0", "req-1#1"
    ]


def test_add_deadline_returns_503_and_resumes(aml, monkeypatch):
    aml.api = FakeApi()
    monkeypatch.setattr(aml, "ADD_DEADLINE_S", -1.0)
    with pytest.raises(aml.HttpError) as err:
        aml.handle_add(_add_body())
    assert err.value.status == 503 and aml.api.adds == []
    monkeypatch.setattr(aml, "ADD_DEADLINE_S", 1500.0)
    aml.handle_add(_add_body())
    assert len(aml.api.adds) == 2


def test_scope_user_is_injective_and_backend_safe(aml):
    safe = aml.scope_user("alice-01")
    assert safe == "u-alice-01"
    nasty = ["a/b", "a'b", 'a"b', "*", "..", ".", "a b", "用户", "a\x00b", "x" * 300, "A", "a"]
    mapped = [aml.scope_user(u) for u in nasty]
    assert len(set(mapped)) == len(nasty), "distinct user_ids must stay distinct"
    for value in mapped:
        assert aml._SAFE_USER_ID.fullmatch(value[2:]) or value.startswith("x-")
        assert all(c.isalnum() or c in "-_.@" for c in value), value
    assert aml.scope_user("x-" + "0" * 40) == "u-x-" + "0" * 40, "prefixes keep spaces disjoint"


def test_http_chunked_request_body(server):
    import http.client

    conn = http.client.HTTPConnection(server.split("//")[1], timeout=10)
    payload = json.dumps(_add_body()).encode()
    conn.putrequest("POST", "/add")
    conn.putheader("X-Api-Key", "k")
    conn.putheader("Transfer-Encoding", "chunked")
    conn.endheaders()
    conn.send(f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n")
    resp = conn.getresponse()
    assert resp.status == 200 and json.loads(resp.read())["request_id"] == "req-1"


def test_http_rejection_with_body_still_returns_status(server):
    big = {"request_id": "r", "messages": [{"role": "user", "content": "x" * 2_000_000}]}
    status, _ = _call(server + "/add", big)  # 无密钥：必须是 401，而不是连接被重置
    assert status == 401


# --------------------------------------------- 真实引擎并发：隔离与不重复 ---


def test_real_engine_parallel_users_stay_isolated(aml, monkeypatch):
    monkeypatch.undo()
    from concurrent.futures import ThreadPoolExecutor

    from jiuwen_memory.api import assemble_runtime, build_dev_authenticator

    runtime = assemble_runtime()
    try:
        aml.api = runtime.api
        aml._authenticator = build_dev_authenticator()
        users = [f"user-{i}" for i in range(12)] + ["we/ird'\"id", "用户"]

        def work(uid):
            for chunk in range(3):
                aml.handle_add(
                    _add_body(
                        request_id=f"{uid}-{chunk}",
                        user_id=uid,
                        messages=[
                            {"role": "user", "content": f"{uid} fact{chunk} zebra",
                             "timestamp": 1700000000000 + chunk},
                        ],
                    )
                )
            return uid, aml.handle_search({"query": "zebra", "user_id": uid, "top_k": 50})

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(work, users))
        for uid, out in results:
            texts = [d["content"] for d in out["data"]]
            assert len(texts) == 3, f"{uid}: expected its own 3 memories, got {len(texts)}"
            assert all(uid in t for t in texts), f"{uid}: foreign memory leaked into results"
    finally:
        runtime.close()


def test_real_engine_same_user_concurrent_adds_no_duplicates(aml, monkeypatch):
    monkeypatch.undo()
    from concurrent.futures import ThreadPoolExecutor

    from jiuwen_memory.api import assemble_runtime, build_dev_authenticator

    runtime = assemble_runtime()
    try:
        aml.api = runtime.api
        aml._authenticator = build_dev_authenticator()

        def add(i):
            # 同一个 request_id 被并发重试：只能写一次，其余要么 409 要么直接重放 200
            try:
                aml.handle_add(_add_body(messages=[{"role": "user", "content": "one fact lemur"}]))
            except aml.HttpError as exc:
                assert exc.status == 409

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(add, range(6)))
        out = aml.handle_search({"query": "lemur", "user_id": "u1", "top_k": 50})
        assert len(out["data"]) == 1, f"retries duplicated the memory: {len(out['data'])}"
    finally:
        runtime.close()
