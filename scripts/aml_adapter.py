# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""AML (agentmemoryleaderboard.ai) Add/Search 协议适配：进程内调用 MemoryAPI。

写入/检索口径对齐 evaluation/locomo/run.py：逐条 message 调 add(infer)，检索取 L2 全文，
created_at 经 inspect 反查 temporal.t_event。

    AML_ADAPTER_KEY=... AML_CONFIG=evaluation/locomo/config.yml \
        python3 scripts/aml_adapter.py --host 0.0.0.0 --port 8200
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import RateLimitError  # noqa: E402

from evaluation.shared.config_loader import load_layer  # noqa: E402
from jiuwen_memory.api import (  # noqa: E402
    RateLimitedError,
    RequestSecurityContext,
    Surface,
    ValidationError,
    assemble_runtime,
)
from jiuwen_memory.common.security import new_request_context  # noqa: E402
from jiuwen_memory.common.security.types import AuthContext  # noqa: E402
from jiuwen_memory.common.type_def import Context, Scope  # noqa: E402
from jiuwen_memory.config import Config  # noqa: E402
from jiuwen_memory.retrieval.types import DisclosureLevel  # noqa: E402

logger = logging.getLogger("aml-adapter")

API_KEY = os.environ.get("AML_ADAPTER_KEY", "")
ORG = os.environ.get("AML_ORG", "aml")
MAX_TOP_K = 100
DATE_PREFIX = os.environ.get("AML_DATE_PREFIX", "1") == "1"
OPTIONS_IN_QUERY = os.environ.get("AML_OPTIONS_IN_QUERY", "0") == "1"
_slots = threading.BoundedSemaphore(int(os.environ.get("AML_MAX_CONCURRENCY", "16")))

# request_id -> 已写入的 message 数；重试从断点续写，不重复落库。进程内即可，重启丢失可接受。
_progress: dict[str, int] = {}
_done: set[str] = set()
_inflight: set[str] = set()
_ledger_lock = threading.Lock()

api = None  # main() 装配后赋值


class _Busy(Exception):
    def __init__(self, status: int):
        super().__init__(status)
        self.status = status


def _security(scope: Scope) -> RequestSecurityContext:
    # 认证边界即本适配层的 AML_ADAPTER_KEY 校验；通过后按 AML user_id 自述身份（同 examples/quickstart.py）
    return new_request_context(
        AuthContext(
            actor=scope,
            credential_type="internal",
            auth_method="internal",
            authenticated_at=datetime.now(timezone.utc),
        ),
        surface=Surface.HTTP,
    )


def _ms_to_dt(ts) -> datetime | None:
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        return None
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)


def handle_add(req: dict) -> dict:
    rid, uid, sid = req["request_id"], req["user_id"], req["session_id"]
    messages = req["messages"]
    if not isinstance(messages, list) or not messages:
        raise ValidationError("messages must be a non-empty array")
    with _ledger_lock:
        if rid in _inflight:
            raise _Busy(409)
        pending = rid not in _done
        if pending:
            _inflight.add(rid)
    if pending:
        # session 只进 system_metadata：scope 带 session 维写入被鉴权拒绝，且需与检索侧同一 scope
        scope = Scope(org=ORG, user=uid)
        try:
            for i in range(_progress.get(rid, 0), len(messages)):
                msg = messages[i]
                text = str(msg.get("content") or "").strip()
                if text:
                    api.add(
                        f"[{msg.get('role', 'user')}]: {text}", scope,
                        security=_security(scope),
                        system_metadata={"infer": "true", "session": sid},
                        occurred_at=_ms_to_dt(msg.get("timestamp")),
                    )
                _progress[rid] = i + 1
            with _ledger_lock:
                _done.add(rid)
                _progress.pop(rid, None)
        finally:
            with _ledger_lock:
                _inflight.discard(rid)
    return {"success": True, "request_id": rid, "user_id": uid, "session_id": sid}


def handle_search(req: dict) -> dict:
    query, uid = req["query"], req["user_id"]
    if OPTIONS_IN_QUERY and req.get("options"):
        query = f"{query}\n" + "\n".join(map(str, req["options"]))
    scope = Scope(org=ORG, user=uid)
    security = _security(scope)
    top_k = max(1, min(int(req.get("top_k") or MAX_TOP_K), MAX_TOP_K))
    res = api.search(query, Context(scope), security=security, top_k=top_k,
                     disclosure=DisclosureLevel.L2)
    ids = [it.unit_id for it in res.items if it.unit_id]
    when: dict[str, str] = {}
    if ids:
        for unit in api.inspect(ids, scope, security=security):
            # t_event 由抽取给出；原文直写时只有 occurred_at 落成的 t_message
            ts = unit.temporal.t_event or unit.temporal.t_message
            if ts is not None:
                when[unit.id] = ts.isoformat()
    data = []
    for it in res.items:
        created = when.get(it.unit_id, "")
        content = f"[{created[:10]}] {it.content}" if DATE_PREFIX and created else it.content
        data.append({"id": it.unit_id, "content": content, "score": it.score, "created_at": created})
    return {"data": data}


def _authorized(headers) -> bool:
    if not API_KEY:
        return True
    auth = headers.get("Authorization", "")
    token = auth.split(" ", 1)[1] if " " in auth else headers.get("X-Api-Key", "")
    return hmac.compare_digest(token.encode(), API_KEY.encode())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self._send(200, {"status": "ok"})

    def do_POST(self):  # noqa: N802
        route = {"/add": handle_add, "/search": handle_search}.get(self.path.rstrip("/"))
        if route is None:
            self._send(404, {"error": "not found"})
            return
        if not _authorized(self.headers):
            self._send(401, {"error": "unauthorized"})
            return
        if not _slots.acquire(blocking=False):
            self._send(429, {"error": "busy"}, retry_after=5)
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            self._send(200, route(body))
        except (KeyError, TypeError, ValueError, AttributeError, ValidationError) as exc:
            self._send(400, {"error": f"bad request: {exc}"})
        except _Busy as exc:
            self._send(exc.status, {"error": "request in progress"}, retry_after=5)
        except (RateLimitedError, RateLimitError):
            self._send(429, {"error": "rate limited"}, retry_after=30)
        except Exception:  # 其余视为暂时性故障，交给 AML 自动重试
            logger.exception("request failed path=%s", self.path)
            self._send(503, {"error": "temporarily unavailable"})
        finally:
            _slots.release()

    def _send(self, status: int, body: dict, retry_after: int | None = None) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002  # pylint: disable=redefined-builtin
        logger.info("%s " + format, self.client_address[0], *args)


def main() -> int:
    global api
    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="aml-adapter")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8200)
    parser.add_argument(
        "--config", default=os.environ.get("AML_CONFIG", "evaluation/locomo/config.yml")
    )
    args = parser.parse_args()
    if not API_KEY:
        logger.warning("AML_ADAPTER_KEY 未设置：鉴权关闭，仅限本地调试")
    layer = load_layer(args.config)
    runtime = assemble_runtime(config=Config.from_dict(dict(layer.get("memory_api") or layer)))
    api = runtime.api
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    logger.info("aml adapter on http://%s:%s (config=%s)", args.host, args.port, args.config)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        runtime.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
