# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""经 AML 适配层 HTTP 端点端到端跑一道 LongMemEval 题：/add → /search → 答题 → 判分。

模拟 AML 平台的调用方式：每个会话按至多 20 条消息分块 POST /add（每条带会话时间戳，毫秒），
全部写完后用题面 POST /search（top_k=100），再用与 harness 相同的 LongMemEval 答题 / 判分
prompt（LLMJudge）在各 cutoff 下打分，便于与 harness 结果直接对照。

用法（适配层需已启动，建议 AML_DATE_PREFIX=0 以与 harness 的上下文形态一致）::

    python scripts/aml_e2e_one.py --question-id 1d4e3b97 \
        --data evaluation/datasets/longmemeval/shared40.json \
        --env evaluation/environment/.env --url http://127.0.0.1:8200

答题 / 判分模型取自 env 文件的 ``LONGMEMEVAL_CHAT_UPSTREAM_URL`` / ``LONGMEMEVAL_CHAT_MODEL`` /
``LONGMEMEVAL_CHAT_API_KEYS``（逗号分隔取第一个），与 harness 的 QA 代理同一上游。
适配层 key 取自 ``AML_ADAPTER_KEY``。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.longmemeval.metrics.llm_judge import (  # noqa: E402
    LONGMEMEVAL_PROMPT_PROFILE,
    LLMJudge,
    OpenAIChatOptions,
    openai_chat,
)

DATE_FMT = "%Y/%m/%d (%a) %H:%M"
CHUNK = 20  # AML 单次 Add 至多 20 条消息
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 524}


def load_env(path: str) -> None:
    """把 env 文件中尚未设置的变量放进 os.environ（不覆盖已有值）。"""
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def post(url: str, body: dict, key: str, attempts: int = 6) -> dict:
    """POST JSON；按 AML 的可重试状态码退避重试。"""
    data = json.dumps(body).encode("utf-8")
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=1800) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code not in RETRY_STATUS or attempt == attempts:
                raise RuntimeError(f"{url} -> {exc.code}: {detail}") from exc
            wait = int(exc.headers.get("Retry-After") or 2 ** attempt)
            print(f"  {exc.code} on attempt {attempt}, retrying in {wait}s: {detail}")
            time.sleep(wait)
    raise AssertionError("unreachable")


def build_chunks(item: dict) -> list[tuple[str, list[dict]]]:
    """按会话切分为 (session_id, ≤20 条消息) 列表，消息带会话时间戳（无时区按 UTC 解释）。"""
    chunks = []
    for sid, date, turns in zip(
        item["haystack_session_ids"], item["haystack_dates"], item["haystack_sessions"]
    ):
        ts = int(datetime.strptime(date, DATE_FMT).replace(tzinfo=UTC).timestamp() * 1000)
        msgs = [{"role": t["role"], "content": t["content"], "timestamp": ts} for t in turns]
        for start in range(0, len(msgs), CHUNK):
            chunks.append((sid, msgs[start:start + CHUNK]))
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(prog="aml-e2e-one")
    parser.add_argument("--question-id", required=True)
    parser.add_argument("--data", default="evaluation/datasets/longmemeval/shared40.json")
    parser.add_argument("--env", default="evaluation/environment/.env")
    parser.add_argument("--url", default="http://127.0.0.1:8200")
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--cutoffs", default="30,10,20,50")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    load_env(args.env)
    adapter_key = os.environ["AML_ADAPTER_KEY"]
    items = {e["question_id"]: e for e in json.load(open(args.data, encoding="utf-8"))}
    item = items[args.question_id]
    user_id = f"e2e-{args.question_id}-{uuid.uuid4().hex[:8]}"
    base = args.url.rstrip("/")

    chunks = build_chunks(item)
    n_msgs = sum(len(m) for _, m in chunks)
    print(f"[{args.question_id}] {len(item['haystack_sessions'])} sessions, {n_msgs} messages, "
          f"{len(chunks)} add requests, user_id={user_id}")
    add_times = []
    t0 = time.perf_counter()
    for i, (sid, msgs) in enumerate(chunks, 1):
        rid = f"{user_id}-{i:04d}"
        start = time.perf_counter()
        resp = post(f"{base}/add",
                    {"request_id": rid, "user_id": user_id, "session_id": sid, "messages": msgs},
                    adapter_key)
        add_times.append(time.perf_counter() - start)
        echo = (resp.get("request_id"), resp.get("user_id"), resp.get("session_id"))
        if echo != (rid, user_id, sid):
            raise RuntimeError(f"add echo mismatch: {resp}")
        print(f"  add {i}/{len(chunks)} session={sid} msgs={len(msgs)} {add_times[-1]:.1f}s")
    add_total = time.perf_counter() - t0

    start = time.perf_counter()
    res = post(f"{base}/search",
               {"query": item["question"], "user_id": user_id, "top_k": args.top_k}, adapter_key)
    search_s = time.perf_counter() - start
    data = res["data"]
    if len(data) > args.top_k:
        raise RuntimeError(f"search returned {len(data)} > top_k {args.top_k}")
    print(f"  search: {len(data)} results in {search_s:.2f}s")

    upstream = os.environ["LONGMEMEVAL_CHAT_UPSTREAM_URL"].rstrip("/")
    upstream = upstream.removesuffix("/chat/completions")
    model = os.environ["LONGMEMEVAL_CHAT_MODEL"]
    chat_key = os.environ["LONGMEMEVAL_CHAT_API_KEYS"].split(",")[0].strip()

    def chat(category: str, max_tokens_env: str):
        return openai_chat(upstream, model, chat_key, OpenAIChatOptions(
            temperature=0.0, max_tokens=int(os.environ.get(max_tokens_env) or 4096),
            timeout=900.0, audit_category=category))

    judge = LLMJudge(chat=chat("answer", "LONGMEMEVAL_ANSWER_MAX_TOKENS"),
                     judge_chat=chat("judge", "LONGMEMEVAL_JUDGE_MAX_TOKENS"),
                     prompt_profile=LONGMEMEVAL_PROMPT_PROFILE)
    cutoffs = [int(c) for c in args.cutoffs.split(",") if c.strip()]
    scores = judge.score_cutoffs(
        item["question"], item["answer"], [d["content"] for d in data],
        {"question_date": item.get("question_date", ""), "query_id": args.question_id,
         "answer_cutoff": cutoffs[0], "answer_cutoffs": ",".join(map(str, cutoffs)),
         "context_dates": [d.get("created_at", "") for d in data]},
    )
    record = judge.records[0]
    print(f"  add total {add_total:.0f}s (mean {add_total / len(chunks):.1f}s, "
          f"max {max(add_times):.1f}s)")
    print("  scores: " + ", ".join(f"@{c}={scores[c]:.0f}" for c in cutoffs))
    final = record["model_answer"].split("</mem_thinking>")[-1].strip()
    print(f"  answer@{cutoffs[0]}: {final[:400]}")

    out = args.out or f"evaluation/outputs/aml_e2e/{args.question_id}-{int(time.time())}.json"
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({
        "question_id": args.question_id, "user_id": user_id, "n_messages": n_msgs,
        "n_add_requests": len(chunks), "add_seconds": add_times, "add_total_s": add_total,
        "search_s": search_s, "search_results": data, "scores": scores, "record": record,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
