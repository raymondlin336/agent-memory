# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AML (agentmemories.ai) Add/Search 协议适配：进程内调用 MemoryAPI。

写入/检索口径对齐 aml-benchmark 的 LongMemEval 评测（evaluation/longmemeval）：

- 写入：相邻 user+assistant 合并为一个对话轮次（超长轮次按句回退并带前文），每个轮次一次
  ``api.add(infer=true)``；metadata 与 ``LongMemEvalDataset._seed_metadata`` 同构；
  AML 的 ``timestamp``（毫秒）作为 ``occurred_at`` 与 ``observation_date``。
- 进程内启用评测同款 no-source 抽取（``retain_source=false``），``AML_NO_SOURCE=0`` 可关。
- 检索：L2 披露，取 ``t_message`` 作为 ``created_at`` 并（默认）加 ``[YYYY-MM-DD]`` 日期前缀。

幂等：AML 会对 408/409/425/429/5xx/524 重试 Add。``(user_id, request_id)`` 的写入进度落在
sqlite 账本里（``AML_STATE_DB``），重试从断点续写；写完的请求直接回 200。账本丢失 =
重试会重复写入，因此它必须在持久盘上。

    AML_ADAPTER_KEY=... scripts/run-aml.sh --host 0.0.0.0 --port 8200

环境变量见 ``deploy/aml/.env.example``，配置见 ``deploy/aml/config.yml``。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import json
import logging
import math
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from evaluation.longmemeval.adapter import (  # noqa: E402
    LONG_TURN_CONTEXT_KEY,
    LongMemEvalDataset,
)
from evaluation.shared.config_loader import load_layer  # noqa: E402
from jiuwen_memory.api import (  # noqa: E402
    AuthenticationError,
    PermissionDeniedError,
    RateLimitedError,
    ValidationError,
    assemble_runtime,
    build_dev_authenticator,
    internal_context,
)
from jiuwen_memory.common.type_def import Context, FilterClause, FilterOp, Scope  # noqa: E402
from jiuwen_memory.config import Config  # noqa: E402
from jiuwen_memory.retrieval.types import DisclosureLevel  # noqa: E402

try:  # openai 仅在装了 embed/LLM 依赖的部署环境里有；单测环境可缺省
    from openai import RateLimitError as _OpenAIRateLimit
except ImportError:  # pragma: no cover
    _OpenAIRateLimit = RateLimitedError

logger = logging.getLogger("aml-adapter")

API_KEY = os.environ.get("AML_ADAPTER_KEY", "")
ORG = os.environ.get("AML_ORG", "aml")
DEFAULT_TOP_K = 100
MAX_TOP_K = int(os.environ.get("AML_MAX_TOP_K", "1000"))
MAX_BODY_BYTES = 64 * 1024 * 1024
DATE_PREFIX = os.environ.get("AML_DATE_PREFIX", "1") == "1"
OPTIONS_IN_QUERY = os.environ.get("AML_OPTIONS_IN_QUERY", "0") == "1"
NO_SOURCE = os.environ.get("AML_NO_SOURCE", "1") == "1"
# 单次 Add 的软截止：AML 单请求最长 30 分钟，超过这个时长就在 group 边界回 503，
# 让 AML 重试并从账本断点续写，而不是被它硬判超时。
ADD_DEADLINE_S = float(os.environ.get("AML_ADD_DEADLINE_S", "1500"))
DIALOGUE_TURN_MAX_CHARS = int(os.environ.get("AML_DIALOGUE_TURN_MAX_CHARS", "4096"))
# Add 动辄数分钟，Search 很短；分池避免长 Add 占满槽位把 Search 挤成 429。
_add_slots = threading.BoundedSemaphore(int(os.environ.get("AML_MAX_ADD_CONCURRENCY", "8")))
_search_slots = threading.BoundedSemaphore(
    int(os.environ.get("AML_MAX_SEARCH_CONCURRENCY", "32"))
)

# 切分与 metadata 直接复用评测适配器的实现，保证 AML 写入形态与基准测过的完全一致。
# 路径不存在 → 数据集不加载，只用它的分组 / metadata 方法。
_GROUPER = LongMemEvalDataset(
    path="<aml-no-dataset>",
    granularity="dialogue_turn",
    infer=True,
    dialogue_turn_max_chars=DIALOGUE_TURN_MAX_CHARS,
)
_DATE_FMT = "%Y/%m/%d (%a) %H:%M"  # 与 LongMemEval session_date 同格式，抽取 prompt 依赖它

api: Any = None  # main() 装配后赋值
_authenticator: Any = None
_ledger: Ledger | None = None  # main() 赋值


class HttpError(Exception):
    """带状态码的请求级错误；``retry_after`` 非空时写入 Retry-After 头。"""

    def __init__(self, status: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


# ---------------------------------------------------------------- 幂等账本 ---


class Ledger:
    """``(user_id, request_id)`` → 写入进度，sqlite 持久化。

    ``payload_hash`` 绑定请求内容：同 ID 但内容不同视为新请求（进度归零），否则会把
    不同内容的 chunk 误判为已写完而静默丢数据。
    """

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS requests ("
            " user_id TEXT NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,"
            " groups_done INTEGER NOT NULL DEFAULT 0, done INTEGER NOT NULL DEFAULT 0,"
            " updated_at REAL NOT NULL, in_progress INTEGER NOT NULL DEFAULT -1,"
            " PRIMARY KEY (user_id, request_id))"
        )
        with contextlib.suppress(sqlite3.OperationalError):  # 旧库补列
            self._db.execute(
                "ALTER TABLE requests ADD COLUMN in_progress INTEGER NOT NULL DEFAULT -1"
            )

    def begin(self, uid: str, rid: str, payload_hash: str) -> tuple[bool, int, int]:
        """返回 ``(已完成, 已写入的 group 数, 写到一半的 group 下标或 -1)``。

        不存在或内容变化则新建 / 重置。``in_progress`` 是写前标记：进程在 ``api.add``
        与进度提交之间崩溃时，它指出哪个 group 可能已落库但没记账。
        """
        with self._lock:
            row = self._db.execute(
                "SELECT payload_hash, groups_done, done, in_progress FROM requests"
                " WHERE user_id=? AND request_id=?",
                (uid, rid),
            ).fetchone()
            if row is not None and row[0] == payload_hash:
                return bool(row[2]), int(row[1]), int(row[3])
            if row is not None:
                logger.warning(
                    "request_id reused with different payload, resetting user=%s rid=%s", uid, rid
                )
            self._db.execute(
                "INSERT OR REPLACE INTO requests VALUES (?, ?, ?, 0, 0, ?, -1)",
                (uid, rid, payload_hash, time.time()),
            )
            return False, 0, -1

    def mark_started(self, uid: str, rid: str, group_index: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE requests SET in_progress=? WHERE user_id=? AND request_id=?",
                (group_index, uid, rid),
            )

    def progress(self, uid: str, rid: str, groups_done: int, *, done: bool = False) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE requests SET groups_done=?, done=?, updated_at=?, in_progress=-1"
                " WHERE user_id=? AND request_id=?",
                (groups_done, int(done), time.time(), uid, rid),
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()


# ----------------------------------------------------------- 请求解析 / 切分 ---


@dataclass(frozen=True)
class _Message:
    role: str
    content: str
    ts: datetime | None


@dataclass(frozen=True)
class _AddRequest:
    request_id: str
    user_id: str
    session_id: str
    messages: list[_Message]
    payload_hash: str


def _require_str(req: dict, key: str) -> str:
    value = req.get(key)
    if not isinstance(value, str) or not value:
        raise HttpError(422, f"{key} must be a non-empty string")
    return value


def _ms_to_dt(ts: Any) -> datetime | None:
    """毫秒时间戳 → 无时区 UTC datetime（评测写入用的就是无时区 datetime）。"""
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(ts / 1000, tz=UTC).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def parse_add(req: Any) -> _AddRequest:
    if not isinstance(req, dict):
        raise HttpError(400, "body must be a JSON object")
    rid, uid, sid = (_require_str(req, k) for k in ("request_id", "user_id", "session_id"))
    raw = req.get("messages")
    if not isinstance(raw, list) or not raw:
        raise HttpError(422, "messages must be a non-empty array")
    messages = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HttpError(422, f"messages[{i}] must be an object")
        role = item.get("role")
        content = item.get("content")
        messages.append(
            _Message(
                role=role if isinstance(role, str) and role else "user",
                content=content if isinstance(content, str) else str(content or ""),
                ts=_ms_to_dt(item.get("timestamp")),
            )
        )
    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    return _AddRequest(rid, uid, sid, messages, digest)


def _fill_timestamps(stamps: list[datetime | None]) -> list[datetime | None]:
    """缺时间戳的消息沿用前一条（无前条则取后一条）；全缺则全为 None（交给引擎默认）。"""
    known = [s for s in stamps if s is not None]
    if not known:
        return list(stamps)
    filled: list[datetime | None] = []
    last = None
    for s in stamps:
        last = s if s is not None else last
        filled.append(last)
    first = known[0]
    return [s if s is not None else first for s in filled]


@dataclass(frozen=True)
class _WriteGroup:
    content: str
    metadata: dict[str, str]
    occurred_at: datetime | None


def build_groups(parsed: _AddRequest) -> list[_WriteGroup]:
    """把一个 chunk 切成与评测同口径的写入单元（dialogue_turn，超长按句回退）。"""
    kept = [m for m in parsed.messages if m.content.strip()]
    if not kept:
        return []
    stamps = _fill_timestamps([m.ts for m in kept])
    turns = [{"role": m.role, "content": m.content} for m in kept]
    groups = []
    for index, group in enumerate(_GROUPER._ingest_groups(turns)):  # noqa: SLF001
        occurred = stamps[group.start]
        date_text = occurred.strftime(_DATE_FMT) if occurred else ""
        metadata = _GROUPER._seed_metadata(  # noqa: SLF001
            parsed.session_id, group.granularity, date_text
        )
        metadata.update(
            {
                "turn_start": str(group.start),
                "turn_end": str(group.end),
                "turn_roles": ",".join(str(t.get("role", "")) for t in group.turns),
                "seed_key": f"{parsed.request_id}#{index}",
            }
        )
        if group.context:
            metadata[LONG_TURN_CONTEXT_KEY] = group.context
        groups.append(
            _WriteGroup(
                content="\n".join(_GROUPER._turn_content(t) for t in group.turns),  # noqa: SLF001
                metadata=metadata,
                occurred_at=occurred,
            )
        )
    return groups


# -------------------------------------------------------------------- Add ---

_inflight: set[tuple[str, str]] = set()
_inflight_lock = threading.Lock()
_user_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
_user_locks_guard = threading.Lock()


_SAFE_USER_ID = re.compile(r"[A-Za-z0-9_.@-]{1,128}")


def scope_user(uid: str) -> str:
    """AML ``user_id`` → 引擎 ``Scope.user``（单射）。

    ``user_id`` 是唯一隔离边界，而引擎把它拼进 Redis 键、ES / Milvus 过滤表达式。引号、斜杠、
    通配符、控制字符等不受控字符一旦进入这些后端语法，隔离就依赖各后端的转义是否完备。
    因此只放行保守字符集（加 ``u-`` 前缀），其余一律取 SHA-256 前 160 位（加 ``x-`` 前缀）；
    两个前缀互不相交，映射保持单射。
    """
    if _SAFE_USER_ID.fullmatch(uid) and uid not in {".", ".."}:
        return f"u-{uid}"
    return "x-" + hashlib.sha256(uid.encode("utf-8")).hexdigest()[:40]


def _group_written(scope: Scope, seed_key: str) -> bool:
    """崩溃恢复：引擎里是否已有携带该 ``seed_key`` 的记忆（派生记忆继承写入 metadata）。

    查询失败按“未写入”处理——宁可重复一个 group，也不丢数据。
    """
    try:
        res = api.list(
            scope,
            security=_security(),
            limit=1,
            filters=[FilterClause("seed_key", FilterOp.EQ, seed_key)],
        )
    except Exception:  # noqa: BLE001
        logger.warning("seed_key probe failed, will rewrite seed_key=%s", seed_key, exc_info=True)
        return False
    return bool(res.items)


def _user_lock(uid: str) -> threading.Lock:
    with _user_locks_guard:
        return _user_locks[uid]


def _security():
    # 进程内受控上下文：身份由 authenticator 产出，业务 scope 只作 target（同评测 harness）。
    # 认证边界是本适配层的 AML_ADAPTER_KEY 校验；用户隔离靠每次调用显式传入的 Scope(user=uid)。
    return internal_context(_authenticator)


def handle_add(req: Any) -> dict:
    parsed = parse_add(req)
    key = (parsed.user_id, parsed.request_id)
    with _inflight_lock:
        if key in _inflight:  # 上一次尝试还在写（客户端超时后重试）：让 AML 稍后再来
            raise HttpError(409, "request in progress", retry_after=5)
        _inflight.add(key)
    started = time.monotonic()
    try:
        # 同一 user 的写入串行：保持 chunk 顺序，避免并发抽取/去重互相干扰。
        with _user_lock(parsed.user_id):
            done, start, crashed = _ledger.begin(
                parsed.user_id, parsed.request_id, parsed.payload_hash
            )
            if done:
                logger.info("add replay user=%s rid=%s", parsed.user_id, parsed.request_id)
                return _echo(parsed)
            groups = build_groups(parsed)
            scope = Scope(org=ORG, user=scope_user(parsed.user_id))
            for gi in range(start, len(groups)):
                group = groups[gi]
                if time.monotonic() - started > ADD_DEADLINE_S:
                    raise HttpError(503, "add deadline reached, retry to resume", retry_after=1)
                if gi == crashed and _group_written(scope, group.metadata["seed_key"]):
                    logger.info("group already persisted, skipping rid=%s group=%d",
                                parsed.request_id, gi)
                    _ledger.progress(parsed.user_id, parsed.request_id, gi + 1)
                    continue
                _ledger.mark_started(parsed.user_id, parsed.request_id, gi)
                api.add(
                    group.content,
                    scope,
                    security=_security(),
                    tags=[parsed.session_id],
                    system_metadata=group.metadata,
                    occurred_at=group.occurred_at,
                )
                _ledger.progress(parsed.user_id, parsed.request_id, gi + 1)
            _ledger.progress(parsed.user_id, parsed.request_id, len(groups), done=True)
        logger.info(
            "add ok user=%s rid=%s msgs=%d groups=%d resumed_from=%d %.1fs",
            parsed.user_id, parsed.request_id, len(parsed.messages), len(groups), start,
            time.monotonic() - started,
        )
        return _echo(parsed)
    finally:
        with _inflight_lock:
            _inflight.discard(key)


def _echo(parsed: _AddRequest) -> dict:
    return {
        "success": True,
        "request_id": parsed.request_id,
        "user_id": parsed.user_id,
        "session_id": parsed.session_id,
    }


# ----------------------------------------------------------------- Search ---


def _parse_top_k(value: Any) -> int:
    if value is None:
        return DEFAULT_TOP_K
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HttpError(422, "top_k must be a positive integer")
    return min(value, MAX_TOP_K)


def handle_search(req: Any) -> dict:
    if not isinstance(req, dict):
        raise HttpError(400, "body must be a JSON object")
    query = _require_str(req, "query")
    uid = _require_str(req, "user_id")
    top_k = _parse_top_k(req.get("top_k"))
    options = req.get("options")
    if OPTIONS_IN_QUERY and isinstance(options, list) and options:
        query = f"{query}\n" + "\n".join(map(str, options))
    scope = Scope(org=ORG, user=scope_user(uid))
    security = _security()
    res = api.search(query, Context(scope), security=security, top_k=top_k,
                     disclosure=DisclosureLevel.L2)
    ids = [it.unit_id for it in res.items if it.unit_id]
    when: dict[str, datetime] = {}
    if ids:
        for unit in api.inspect(ids, scope, security=security):
            # t_message 是对话发生时刻（评测同口径）；原文直写没有 t_event 时也有它
            ts = unit.temporal.t_message or unit.temporal.t_event
            if ts is not None:
                when[unit.id] = ts
    data, seen = [], set()
    for it in res.items:
        if not it.unit_id or it.unit_id in seen or not (it.content or "").strip():
            continue  # 契约要求 id / content 非空；id 稳定且不重复
        seen.add(it.unit_id)
        ts = when.get(it.unit_id)
        content = f"[{ts:%Y-%m-%d}] {it.content}" if DATE_PREFIX and ts else it.content
        entry: dict[str, Any] = {"id": it.unit_id, "content": content}
        if isinstance(it.score, (int, float)) and math.isfinite(it.score):
            entry["score"] = float(it.score)
        if ts is not None:
            entry["created_at"] = ts.isoformat()
        data.append(entry)
    return {"data": data[:top_k]}  # 超出 top_k 即契约失败，硬截断


# ------------------------------------------------------------------- HTTP ---


def _authorized(headers) -> bool:
    """Token / Bearer（``Authorization: <scheme> <key>``）或 ``X-Api-Key``，任一匹配即可。"""
    if not API_KEY:
        return True
    candidates = []
    auth = (headers.get("Authorization") or "").strip()
    if auth:
        parts = auth.split(None, 1)
        candidates.append(parts[1] if len(parts) == 2 else parts[0])
    if headers.get("X-Api-Key"):
        candidates.append(headers["X-Api-Key"])
    key = API_KEY.encode("utf-8")
    return any(hmac.compare_digest(c.strip().encode("utf-8"), key) for c in candidates)


def _classify(exc: Exception) -> HttpError:
    """引擎 / 依赖异常 → HTTP 状态。只有确定性的输入问题回 4xx，其余交给 AML 重试。"""
    if isinstance(exc, ValidationError):
        return HttpError(422, f"invalid input: {exc}")
    if isinstance(exc, (RateLimitedError, _OpenAIRateLimit)):
        return HttpError(429, "rate limited", retry_after=30)
    if isinstance(exc, (PermissionDeniedError, AuthenticationError)):
        # 适配层与引擎之间的授权问题，不是客户端的错；大声记日志，回 500 让重试暴露而非吞掉
        logger.error("engine authorization failure: %s", exc)
        return HttpError(500, "engine authorization failure")
    logger.exception("request failed")
    return HttpError(503, "temporarily unavailable")


class Handler(BaseHTTPRequestHandler):
    timeout = 120  # 单次 socket 读写超时；处理耗时不受限（Add 可达数分钟）

    def do_GET(self):  # noqa: N802
        self._send(200, {"status": "ok"})  # 健康检查：免鉴权，任意路径

    def do_POST(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/")
        route = {"/add": handle_add, "/search": handle_search}.get(path)
        # 先把请求体读完再决定回什么：带着未读完的请求体就关连接，客户端会收到连接重置
        # 而不是 401 / 404 / 429 状态码，AML 就无法按状态码分类重试。
        try:
            raw = self._read_body()
        except HttpError as exc:
            self._send(exc.status, {"error": exc.message})
            return
        except (ValueError, OSError):
            self._send(400, {"error": "malformed request body"})
            return
        if route is None:
            self._send(404, {"error": "not found"})
            return
        if not _authorized(self.headers):
            self._send(401, {"error": "unauthorized"})
            return
        slots = _add_slots if path == "/add" else _search_slots
        if not slots.acquire(blocking=False):
            self._send(429, {"error": "busy"}, retry_after=5)
            return
        try:
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError) as exc:
                raise HttpError(400, f"invalid JSON: {exc}") from exc
            self._send(200, route(body))
        except HttpError as exc:
            self._send(exc.status, {"error": exc.message}, retry_after=exc.retry_after)
        except Exception as exc:  # noqa: BLE001
            err = _classify(exc)
            self._send(err.status, {"error": err.message}, retry_after=err.retry_after)
        finally:
            slots.release()

    def _read_body(self) -> bytes:
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            chunks, total = [], 0
            while True:
                size = int(self.rfile.readline().split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline().strip():  # trailer
                        pass
                    return b"".join(chunks)
                total += size
                if total > MAX_BODY_BYTES:
                    raise HttpError(413, "body too large")
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            raise HttpError(413, "body too large")
        return self.rfile.read(length)

    def _send(self, status: int, body: dict, retry_after: int | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            if retry_after is not None:
                self.send_header("Retry-After", str(retry_after))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # Add 跑久了客户端可能已断开；写入已记账，AML 重试会命中账本直接回 200
            logger.warning("client went away before response status=%s", status)

    def log_message(self, format, *args):  # noqa: A002  # pylint: disable=redefined-builtin
        logger.info("%s " + format, self.client_address[0], *args)


def selfcheck() -> None:
    """启动自检：用适配层的安全上下文做一次只读调用，授权 / 存储连不通就立刻失败。"""
    api.list(Scope(org=ORG, user="__aml_selfcheck__"), security=_security(), limit=1)


def main() -> int:
    global api, _authenticator, _ledger
    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="aml-adapter")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument("--config", default=os.environ.get("AML_CONFIG"))
    args = parser.parse_args()
    if not args.config:
        parser.error("必须通过 --config 或 AML_CONFIG 指定配置（默认见 deploy/aml/config.yml）")
    if not API_KEY and os.environ.get("AML_ALLOW_NO_AUTH") != "1":
        parser.error("AML_ADAPTER_KEY 未设置：拒绝无鉴权启动（本地调试可设 AML_ALLOW_NO_AUTH=1）")
    layer = load_layer(args.config)
    with contextlib.ExitStack() as stack:
        if NO_SOURCE:
            from evaluation.longmemeval.no_source import no_source_extraction

            stack.enter_context(no_source_extraction())  # 进程生命周期内生效
        runtime = assemble_runtime(config=Config.from_dict(dict(layer.get("memory_api") or layer)))
        stack.callback(runtime.close)
        api = runtime.api
        _authenticator = build_dev_authenticator()
        _ledger = Ledger(os.environ.get("AML_STATE_DB", "aml_adapter_state.db"))
        stack.callback(_ledger.close)
        selfcheck()
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
        httpd.daemon_threads = True
        logger.info(
            "aml adapter on http://%s:%s (config=%s no_source=%s)",
            args.host, args.port, args.config, NO_SOURCE,
        )
        # 容器 / systemd 停机发 SIGTERM：走正常退出路径，让引擎与账本干净关闭。
        # shutdown() 要在 serve_forever 之外的线程调用，否则死锁。
        if threading.current_thread() is threading.main_thread():
            signal.signal(
                signal.SIGTERM,
                lambda *_: threading.Thread(target=httpd.shutdown, daemon=True).start(),
            )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
