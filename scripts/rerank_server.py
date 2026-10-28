# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Local cross-encoder rerank server with a Cohere-style API (CPU).

Serves ``POST /v1/rerank`` for JiuwenMemory's ``api`` reranker (``reranker_dialect: cohere``):

    {"model": ..., "query": "...", "documents": ["...", ...], "top_n": 300}
    -> {"results": [{"index": 3, "relevance_score": 0.97}, ...]}   # best first

Scores are ``sigmoid(logit)`` of ``cross-encoder/ms-marco-MiniLM-L-6-v2`` (in (0, 1)), so the
engine's ">0" score threshold keeps every candidate. ``GET`` on any path is a health check.

    python scripts/rerank_server.py --port 8906

Needs ``torch`` and ``transformers`` (``pip install -e ".[rerank]"``). The model is downloaded
from Hugging Face on first start and cached; set ``HF_HUB_OFFLINE=1`` to use only the cache.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
BATCH_SIZE = 32
MAX_BODY_BYTES = 32 * 1024 * 1024

logger = logging.getLogger("rerank-server")


class CrossEncoderScorer:
    """Scores (query, document) pairs with a sequence-classification cross-encoder."""

    def __init__(self, model_name: str, max_length: int = 512):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self._torch = torch
        self.model_name = model_name
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name).eval()
        self.max_length = min(max_length, self.tokenizer.model_max_length or max_length)
        self._lock = threading.Lock()  # one forward pass at a time; torch already uses all cores

    def logits(self, query: str, documents: list[str]) -> list[float]:
        out: list[float] = []
        with self._lock, self._torch.inference_mode():
            for start in range(0, len(documents), BATCH_SIZE):
                batch = documents[start:start + BATCH_SIZE]
                enc = self.tokenizer(
                    [query] * len(batch), batch, padding=True, truncation=True,
                    max_length=self.max_length, return_tensors="pt",
                )
                out.extend(self.model(**enc).logits[:, 0].tolist())
        return out


def rerank_response(scorer, body: dict) -> dict:
    """Builds the Cohere-style response for one rerank request body."""
    query = body.get("query")
    docs = body.get("documents")
    if not isinstance(query, str) or not isinstance(docs, list):
        raise ValueError("query must be a string and documents a list")
    # Cohere also accepts {"text": ...} objects as documents.
    texts = [d.get("text", "") if isinstance(d, dict) else str(d) for d in docs]
    logits = scorer.logits(query, texts) if texts else []
    results = [
        {"index": i, "relevance_score": 1.0 / (1.0 + math.exp(-x))} for i, x in enumerate(logits)
    ]
    results.sort(key=lambda r: r["relevance_score"], reverse=True)
    top_n = body.get("top_n")
    if isinstance(top_n, int) and not isinstance(top_n, bool) and top_n > 0:
        results = results[:top_n]
    return {"results": results}


def make_handler(scorer):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, obj: dict) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            self._send(200, {"status": "ok", "model": scorer.model_name})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self._send(413, {"error": "body too large"})
                return
            raw = self.rfile.read(length)
            if not self.path.split("?", 1)[0].rstrip("/").endswith("/rerank"):
                self._send(404, {"error": f"unknown path {self.path}"})
                return
            try:
                body = json.loads(raw or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                self._send(200, rerank_response(scorer, body))
            except ValueError as exc:
                self._send(400, {"error": str(exc)})
            except Exception:  # noqa: BLE001
                logger.exception("rerank failed")
                self._send(500, {"error": "rerank failed"})

        def log_message(self, format, *args):  # noqa: A002  # pylint: disable=redefined-builtin
            pass  # request bodies are evaluation data; keep the log quiet

    return Handler


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="[%(asctime)s] %(name)s %(levelname)s %(message)s"
    )
    parser = argparse.ArgumentParser(prog="rerank-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8906)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args()
    scorer = CrossEncoderScorer(args.model)
    httpd = ThreadingHTTPServer((args.host, args.port), make_handler(scorer))
    httpd.daemon_threads = True
    logger.info("rerank %s on http://%s:%s/v1", args.model, args.host, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
