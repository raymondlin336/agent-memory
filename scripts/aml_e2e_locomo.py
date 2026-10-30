# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run a LoCoMo subset end to end through the AML adapter.

Flow: /add -> /search -> answer -> judge. Calls the adapter the way the AML platform does: each
session is POSTed to /add in chunks of at most 20 messages or 2,000 words (each message carrying
the session timestamp in ms). After all writes, each question is POSTed to /search (top_k=100),
then answered and judged at each cutoff with evaluation/locomo/prompts.py. Speakers map as in the
harness: speaker_a -> user, speaker_b -> assistant, with names replaced in text and questions.

Usage (results stay in --out; rerun with the same --run-id to resume after a failure, since Adds
already recorded are skipped and the adapter treats repeated request_ids as replays)::

    python scripts/aml_e2e_locomo.py --url http://HOST:8901 --convs 0,1,8 --qa-per-conv 9 \
        --run-id subset1 --out evaluation/outputs/aml_e2e/locomo_server/subset1

``--env`` defaults to deploy/aml/.env. The answer model comes from ``API_BASE`` / ``MODEL_NAME`` /
``API_KEY``; the judge uses ``JUDGE_BASE_URL`` / ``JUDGE_MODEL`` / ``JUDGE_API_KEY`` when all are
set, otherwise the answer model. The adapter key comes from ``AML_ADAPTER_KEY`` (required).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.locomo import prompts  # noqa: E402
from evaluation.longmemeval.metrics.llm_judge import (  # noqa: E402
    OpenAIChatOptions,
    openai_chat,
)
from scripts.aml_e2e_one import _TRANSPORT_ERRORS, CHUNK, RETRY_STATUS, load_env  # noqa: E402

MAX_WORDS = 2000  # AML sends at most 2,000 words per Add
DATE_FMT = "%I:%M %p on %d %B, %Y"  # same as evaluation/locomo/run.py
DATE_PREFIX = re.compile(r"^\[\d{4}-\d{2}-\d{2}\] ")
ATTEMPTS = 8

_stats_lock = threading.Lock()
_status_counts: Counter = Counter()  # per endpoint and status: "add:200", "search:transport", ...
_retry_counts: Counter = Counter()
_contract_problems: list[str] = []


def _count(endpoint: str, status: str, retried: bool = False) -> None:
    with _stats_lock:
        _status_counts[f"{endpoint}:{status}"] += 1
        if retried:
            _retry_counts[endpoint] += 1


def _problem(text: str) -> None:
    with _stats_lock:
        _contract_problems.append(text)
    print(f"  CONTRACT PROBLEM: {text}", flush=True)


def post(url: str, body: dict, key: str, endpoint: str) -> dict:
    """POST JSON with AML's bounded retries, counting every status and retry."""
    data = json.dumps(body).encode("utf-8")
    for attempt in range(1, ATTEMPTS + 1):
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=1800) as resp:
                payload = resp.read()
                _count(endpoint, str(resp.status))
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except _TRANSPORT_ERRORS:
                detail = "(error body lost: connection dropped)"
            retry = exc.code in RETRY_STATUS and attempt < ATTEMPTS
            _count(endpoint, str(exc.code), retried=retry)
            if not retry:
                raise RuntimeError(f"{url} -> {exc.code}: {detail}") from exc
            wait = int(exc.headers.get("Retry-After") or min(2 ** attempt, 60))
            print(f"  {exc.code} on attempt {attempt}, retrying in {wait}s: {detail}", flush=True)
        except _TRANSPORT_ERRORS as exc:
            retry = attempt < ATTEMPTS
            _count(endpoint, "transport", retried=retry)
            if not retry:
                raise RuntimeError(f"{url} -> transport failure: {exc!r}") from exc
            wait = min(2 ** attempt, 60)
            print(f"  transport failure on attempt {attempt} ({type(exc).__name__}), "
                  f"retrying in {wait}s", flush=True)
        time.sleep(wait)
    raise AssertionError("unreachable")


def replace_speakers(text: str, speaker_a: str, speaker_b: str) -> str:
    """Same mapping as evaluation/locomo/run.py, which cannot be imported without side effects."""
    pairs = sorted(((speaker_a, "user"), (speaker_b, "assistant")),
                   key=lambda item: len(item[0]), reverse=True)
    for speaker, role in pairs:
        if speaker:
            text = text.replace(speaker, role)
    return text


def parse_label(content: str) -> str:
    """Same label parsing as evaluation/locomo/run.py."""
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        try:
            label = str(json.loads(text[start:end + 1]).get("label", "")).strip().upper()
            if label:
                return label
        except json.JSONDecodeError:
            pass
    if "CORRECT" in text.upper() and "WRONG" not in text.upper():
        return "CORRECT"
    return "WRONG" if "WRONG" in text.upper() else ""


def session_keys(conv: dict) -> list[str]:
    keys = [k for k in conv if k.startswith("session_") and not k.endswith("_date_time")]
    return sorted(keys, key=lambda k: int(k.split("_")[1]))


def reference_date(conv: dict) -> str:
    """Same as the harness: the first parseable date in lexically sorted key order."""
    for key in sorted(conv):
        if key.startswith("session_") and key.endswith("_date_time"):
            try:
                return datetime.strptime(conv[key].strip(), DATE_FMT).strftime("%B %d, %Y")
            except ValueError:
                continue
    return "2023"


def build_chunks(conv: dict, max_sessions: int) -> list[tuple[str, list[dict]]]:
    """Split into (session_id, messages) chunks within AML's 20-message and 2,000-word limits."""
    speaker_a, speaker_b = conv["speaker_a"], conv["speaker_b"]
    keys = session_keys(conv)
    chunks = []
    for sid in keys[:max_sessions] if max_sessions else keys:
        when = datetime.strptime(conv[f"{sid}_date_time"].strip(), DATE_FMT)
        ts = int(when.replace(tzinfo=UTC).timestamp() * 1000)
        current: list[dict] = []
        words = 0
        for turn in conv[sid]:
            text = turn.get("text", "")
            if turn.get("blip_caption"):
                text += ("(The conversation is accompanied by an image, and the "
                         f"description of the image is '{turn['blip_caption']}')")
            text = replace_speakers(text, speaker_a, speaker_b)
            if not text.strip():
                continue
            n_words = len(text.split())
            if current and (len(current) >= CHUNK or words + n_words > MAX_WORDS):
                chunks.append((sid, current))
                current, words = [], 0
            role = "user" if turn.get("speaker") == speaker_a else "assistant"
            current.append({"role": role, "content": text, "timestamp": ts})
            words += n_words
        if current:
            chunks.append((sid, current))
    return chunks


def select_questions(item: dict, limit: int, sessions: set[int] | None) -> list[tuple[int, dict]]:
    """Take questions round-robin over categories 1-4, evenly spaced within each category."""
    by_cat: dict[int, list[tuple[int, dict]]] = {c: [] for c in prompts.CATEGORIES_TO_EVALUATE}
    for index, qa in enumerate(item["qa"]):
        if qa.get("category") not in by_cat or "answer" not in qa:
            continue
        if sessions is not None:  # path check: only questions answerable from what was sent
            needed = {int(m.group(1)) for e in qa.get("evidence", [])
                      for m in [re.match(r"D(\d+):", e)] if m}
            if not needed or not needed <= sessions:
                continue
        by_cat[qa["category"]].append((index, qa))
    picked: list[tuple[int, dict]] = []
    round_no = 0
    while len(picked) < limit and any(len(v) > round_no for v in by_cat.values()):
        for cat in by_cat:
            pool = by_cat[cat]
            if round_no < len(pool) and len(picked) < limit:
                # Spread picks over the conversation with a fixed stride.
                picked.append(pool[(round_no * 7) % len(pool)])
        round_no += 1
    return list({index: (index, qa) for index, qa in picked}.values())


def evidence_text(conv: dict, evidence: list[str]) -> list[str]:
    turns = {t.get("dia_id"): t for key in session_keys(conv) for t in conv[key]}
    out = []
    for ref in evidence:
        turn = turns.get(ref)
        if turn:
            out.append(f"{ref} {turn.get('speaker')}: {turn.get('text')}")
    return out


def write_conversation(ctx: dict, conv_index: int, item: dict) -> dict:
    conv = item["conversation"]
    user_id = f"e2e-locomo-{ctx['run_id']}-{item['sample_id']}"
    chunks = build_chunks(conv, ctx["max_sessions"])
    log_path = ctx["out"] / f"adds_{item['sample_id']}.jsonl"
    done: dict[int, dict] = {}
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            done[record["index"]] = record
    print(f"[{item['sample_id']}] {len(chunks)} add requests, "
          f"{sum(len(m) for _, m in chunks)} messages, {len(done)} already done, "
          f"user_id={user_id}", flush=True)
    for i, (sid, msgs) in enumerate(chunks, 1):
        if i in done:
            continue
        rid = f"{user_id}-{i:04d}"
        start = time.perf_counter()
        resp = post(f"{ctx['base']}/add",
                    {"request_id": rid, "user_id": user_id, "session_id": sid, "messages": msgs},
                    ctx["key"], "add")
        elapsed = time.perf_counter() - start
        echo = (resp.get("request_id"), resp.get("user_id"), resp.get("session_id"))
        if echo != (rid, user_id, sid):
            _problem(f"add echo mismatch on {rid}: {echo}")
        if resp.get("success") is not True:
            _problem(f"add success is not true on {rid}: {resp.get('success')!r}")
        record = {"index": i, "session": sid, "n_messages": len(msgs),
                  "n_words": sum(len(m["content"].split()) for m in msgs), "seconds": elapsed}
        done[i] = record
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"  [{item['sample_id']}] add {i}/{len(chunks)} {sid} msgs={len(msgs)} "
              f"{elapsed:.1f}s", flush=True)
    return {"sample_id": item["sample_id"], "conv_index": conv_index, "user_id": user_id,
            "adds": [done[i] for i in sorted(done)]}


def check_results(data: list[dict], top_k: int, label: str) -> dict:
    if len(data) > top_k:
        _problem(f"{label}: {len(data)} results > top_k {top_k}")
    ids = [d.get("id") for d in data]
    if any(not d.get("id") or not str(d.get("content") or "").strip() for d in data):
        _problem(f"{label}: result missing id or content")
    if len(set(ids)) != len(ids):
        _problem(f"{label}: duplicate ids")
    return {
        "n_results": len(data),
        "n_date_prefixed": sum(bool(DATE_PREFIX.match(str(d.get("content", "")))) for d in data),
        "n_created_at": sum(bool(d.get("created_at")) for d in data),
        "n_score": sum("score" in d for d in data),
    }


def _with_retries(call, *args) -> str:
    for attempt in range(1, 5):
        try:
            return call(*args)
        except Exception as exc:  # noqa: BLE001 - any LLM failure is retried, then surfaces
            if attempt == 4:
                raise
            print(f"  LLM error ({type(exc).__name__}), retry {attempt}", flush=True)
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def run_question(ctx: dict, item: dict, user_id: str, q_index: int, qa: dict) -> dict:
    conv = item["conversation"]
    speaker_a, speaker_b = conv["speaker_a"], conv["speaker_b"]
    category = qa["category"]
    question = replace_speakers(qa["question"], speaker_a, speaker_b)
    gold = replace_speakers(prompts.preprocess_answer(category, str(qa["answer"])),
                            speaker_a, speaker_b)
    start = time.perf_counter()
    res = post(f"{ctx['base']}/search",
               {"query": question, "user_id": user_id, "top_k": ctx["top_k"]},
               ctx["key"], "search")
    search_s = time.perf_counter() - start
    label = f"{item['sample_id']}/q{q_index}"
    data = res.get("data")
    if not isinstance(data, list):
        _problem(f"{label}: search has no data array")
        data = []
    checks = check_results(data, ctx["top_k"], label)
    memories = [{"memory": d.get("content", ""), "created_at": d.get("created_at", "")}
                for d in data]
    ref_date = reference_date(conv)
    per_cutoff = {}
    for cutoff in ctx["cutoffs"]:
        prompt = prompts.get_answer_generation_prompt(question, memories[:cutoff], ref_date)
        response = _with_retries(ctx["answer"], "", prompt)
        verdict = _with_retries(ctx["judge"], prompts.JUDGE_SYSTEM_PROMPT,
                                prompts.get_judge_prompt(category, question, gold, response))
        per_cutoff[str(cutoff)] = {"response": response, "verdict": verdict,
                                   "label": parse_label(verdict)}
    shown = per_cutoff[str(max(ctx["cutoffs"]))]["label"]
    print(f"  [{label}] {prompts.CATEGORY_NAMES[category]} search={search_s:.2f}s "
          f"results={checks['n_results']} {shown}", flush=True)
    return {
        "sample_id": item["sample_id"], "qa_index": q_index, "category": category,
        "category_name": prompts.CATEGORY_NAMES[category], "question": question, "gold": gold,
        "evidence": qa.get("evidence", []),
        "evidence_text": evidence_text(conv, qa.get("evidence", [])),
        "search_s": search_s, "checks": checks, "per_cutoff": per_cutoff, "search_results": data,
    }


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p / 100 * len(ordered)))]


def summarize(ctx: dict, writes: list[dict], results: list[dict], judge_model: str) -> dict:
    add_s = [a["seconds"] for w in writes for a in w["adds"]]
    n_msgs = sum(a["n_messages"] for w in writes for a in w["adds"])
    search_s = [r["search_s"] for r in results]
    accuracy: dict[str, dict] = {}
    for cutoff in ctx["cutoffs"]:
        by_cat: dict[str, list[int]] = {}
        for r in results:
            ok = r["per_cutoff"][str(cutoff)]["label"] == "CORRECT"
            for key in ("overall", r["category_name"]):
                bucket = by_cat.setdefault(key, [0, 0])
                bucket[0] += ok
                bucket[1] += 1
        accuracy[str(cutoff)] = {k: {"correct": v[0], "total": v[1]} for k, v in by_cat.items()}
    return {
        "run_id": ctx["run_id"], "judge_model": judge_model, "answer_model": ctx["answer_model"],
        "top_k": ctx["top_k"], "n_adds": len(add_s), "n_messages": n_msgs,
        "add_s": {"total": sum(add_s), "mean": sum(add_s) / max(len(add_s), 1),
                  "p50": percentile(add_s, 50), "p95": percentile(add_s, 95),
                  "max": max(add_s, default=0.0)},
        "s_per_exchange": sum(add_s) / max(n_msgs / 2, 1),
        "search_s": {"mean": sum(search_s) / max(len(search_s), 1),
                     "p50": percentile(search_s, 50), "p95": percentile(search_s, 95),
                     "max": max(search_s, default=0.0)},
        "status_counts": dict(_status_counts), "retries": dict(_retry_counts),
        "contract_problems": _contract_problems,
        "results_total": sum(r["checks"]["n_results"] for r in results),
        "results_date_prefixed": sum(r["checks"]["n_date_prefixed"] for r in results),
        "results_created_at": sum(r["checks"]["n_created_at"] for r in results),
        "results_score": sum(r["checks"]["n_score"] for r in results),
        "max_results": max((r["checks"]["n_results"] for r in results), default=0),
        "accuracy": accuracy,
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="aml-e2e-locomo")
    parser.add_argument("--data", default="evaluation/datasets/locomo/locomo10.json")
    parser.add_argument("--env", default="deploy/aml/.env")
    parser.add_argument("--url", required=True)
    parser.add_argument("--convs", default="0", help="comma-separated conversation indexes")
    parser.add_argument("--qa-per-conv", type=int, default=9)
    parser.add_argument("--max-sessions", type=int, default=0, help="0 = all sessions")
    parser.add_argument("--concurrency", type=int, default=3, help="at most 8")
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--cutoffs", default="10,20,50,100")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if args.concurrency > 8:
        raise SystemExit("--concurrency must be at most 8")

    if Path(args.env).exists():
        load_env(args.env)
    key = os.environ.get("AML_ADAPTER_KEY")
    if not key:
        raise SystemExit("AML_ADAPTER_KEY is not set")
    upstream = os.environ["API_BASE"].rstrip("/").removesuffix("/chat/completions")
    answer_model = os.environ["MODEL_NAME"]
    answer = openai_chat(upstream, answer_model, os.environ["API_KEY"], OpenAIChatOptions(
        temperature=0.01, max_tokens=2048, timeout=300.0, audit_category="answer"))
    judge_model = answer_model
    judge_cfg = (upstream, answer_model, os.environ["API_KEY"])
    if all(os.environ.get(k) for k in ("JUDGE_BASE_URL", "JUDGE_MODEL", "JUDGE_API_KEY")):
        judge_model = os.environ["JUDGE_MODEL"]
        judge_cfg = (os.environ["JUDGE_BASE_URL"].rstrip("/"), judge_model,
                     os.environ["JUDGE_API_KEY"])
    judge = openai_chat(*judge_cfg, OpenAIChatOptions(
        temperature=0.0, max_tokens=512, timeout=300.0, audit_category="judge"))

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dataset = json.load(open(args.data, encoding="utf-8"))
    items = {i: dataset[i] for i in (int(c) for c in args.convs.split(","))}
    ctx = {"base": args.url.rstrip("/"), "key": key, "run_id": args.run_id, "out": out,
           "max_sessions": args.max_sessions, "top_k": args.top_k, "answer": answer,
           "judge": judge, "answer_model": answer_model,
           "cutoffs": [int(c) for c in args.cutoffs.split(",")]}
    print(f"answer={answer_model} judge={judge_model} convs={list(items)}", flush=True)

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        writes = list(pool.map(lambda kv: write_conversation(ctx, kv[0], kv[1]), items.items()))
    write_wall = time.perf_counter() - t0
    print(f"writes finished in {write_wall:.0f}s wall", flush=True)

    allowed = set(range(1, args.max_sessions + 1)) if args.max_sessions else None
    jobs = []
    for write, item in zip(writes, items.values()):
        for q_index, qa in select_questions(item, args.qa_per_conv, allowed):
            jobs.append((item, write["user_id"], q_index, qa))
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda job: run_question(ctx, *job), jobs))

    summary = summarize(ctx, writes, results, judge_model)
    summary["write_wall_s"] = write_wall
    (out / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
