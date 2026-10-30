"""Capacity ramp for the AML adapter: N users writing real shared40 conversations at once.

Each user sends Adds of at most 20 messages, one at a time, as AML does. There are no retries:
errors are counted, and 429s (busy = adapter slots full, upstream = model rate limit), 5xx and
transport errors are reported separately. Search can optionally run alongside the writes.

Throughput is time-weighted: each Add's exchanges count in proportion to its overlap with the
measurement window, so long Adds (1-4 minutes) still in flight are not missed.

    python scripts/aml_loadtest.py --url http://host:8901 --check
    python scripts/aml_loadtest.py --url http://host:8901 --levels 32,64,96,128,192

The run sheet is evaluation/outputs/capacity/server_test_plan.md.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATE_FMT = "%Y/%m/%d (%a) %H:%M"
ERROR_CLASSES = ("429_busy", "429_upstream", "5xx", "transport", "other_4xx")
MIN_LEVEL_S = 600  # An Add takes 1-4 minutes; shorter levels mostly measure in-flight tails.
ABORT_MIN_REQUESTS = 20
# Ignore proxies set in the environment: requests carry the key, so they go straight to the server.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


@dataclass
class Outcome:
    status: int | None
    error_class: str | None  # None means success
    retry_after: float | None = None
    detail: str = ""
    body: bytes = b""


@dataclass
class AddRecord:
    worker: int
    start: float
    end: float
    status: int | None
    error_class: str | None
    exchanges: int


@dataclass
class SearchRecord:
    start: float
    end: float
    status: int | None
    error_class: str | None
    hits: int


def load_key(env_file: Path) -> str:
    """Read AML_ADAPTER_KEY from the env file. Errors never include the file's contents."""
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("AML_ADAPTER_KEY="):
            key = line.split("=", 1)[1].strip().strip("\"'")
            if key:
                return key
    raise SystemExit(f"AML_ADAPTER_KEY is not set in {env_file}")


def chunks_for(item: dict) -> list[tuple[str, list[dict]]]:
    out = []
    sessions = zip(
        item["haystack_session_ids"], item["haystack_dates"], item["haystack_sessions"], strict=True
    )
    for sid, date, turns in sessions:
        ts = int(datetime.strptime(date, DATE_FMT).replace(tzinfo=UTC).timestamp() * 1000)
        msgs = [{"role": t["role"], "content": t["content"], "timestamp": ts} for t in turns]
        for s in range(0, len(msgs), 20):
            out.append((sid, msgs[s : s + 20]))
    return out


def classify(status: int, body: bytes) -> str | None:
    if status == 200:
        return None
    if status == 429:
        # The adapter answers {"error": "busy"} when slots are full and
        # {"error": "rate limited"} when a model API limits it.
        return "429_busy" if b"busy" in body else "429_upstream"
    return "5xx" if status >= 500 else "other_4xx"


def post(url: str, key: str, path: str, body: dict, timeout: float) -> Outcome:
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return Outcome(resp.status, None, body=resp.read())
    except urllib.error.HTTPError as exc:
        data = exc.read()
        after = exc.headers.get("Retry-After")
        return Outcome(
            exc.code,
            classify(exc.code, data),
            retry_after=float(after) if after and after.isdigit() else None,
            detail=data[:200].decode("utf-8", "replace"),
        )
    except Exception as exc:  # noqa: BLE001  # resets, timeouts etc. are transport errors
        return Outcome(None, "transport", detail=type(exc).__name__)


def health(url: str) -> bool:
    try:
        with _opener.open(url + "/health", timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:  # noqa: BLE001
        print(f"health check failed: {type(exc).__name__}", flush=True)
        return False


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(p * len(ordered)))], 2)


def window_exchanges(records: list[AddRecord], begin: float, end: float) -> float:
    """Exchanges done in the window: successful Adds count by overlap, assuming steady progress."""
    total = 0.0
    for r in records:
        if r.error_class is None and r.end > r.start:
            overlap = min(r.end, end) - max(r.start, begin)
            if overlap > 0:
                total += r.exchanges * overlap / (r.end - r.start)
    return total


def count_errors(records: list[Any]) -> dict[str, int]:
    return {c: sum(r.error_class == c for r in records) for c in ERROR_CLASSES}


class LevelRun:
    """One concurrency level: writer threads plus optional Search threads."""

    def __init__(self, args, key: str, items: list[dict], run_id: str, users: int, pool: list):
        self.args = args
        self.key = key
        self.items = items
        self.run_id = run_id
        self.users = users
        self.pool = pool  # written users, kept across levels for Search
        self.pool_lock = threading.Lock()
        self.lock = threading.Lock()
        self.adds: list[AddRecord] = []
        self.searches: list[SearchRecord] = []
        self.samples: dict[str, list[str]] = {}
        self.inflight = 0
        self.stop = threading.Event()
        self.t0 = 0.0
        self.deadline = 0.0

    def _note_error(self, out: Outcome) -> None:
        samples = self.samples.setdefault(out.error_class or "", [])
        if len(samples) < 5:
            samples.append(f"{out.status}: {out.detail}")

    def _add_worker(self, index: int) -> None:
        # Stagger the starts so all connections do not arrive in the same instant.
        if self.stop.wait(self.args.ramp_s * index / self.users):
            return
        item_index = index % len(self.items)
        # Workers sharing an item start at different points: identical text would hit the
        # embedder cache and understate the load.
        offset = (index // len(self.items)) * 8
        passes = 0
        while True:
            item = self.items[item_index]
            user = f"{self.args.prefix}-{self.run_id}-n{self.users}-w{index}-p{passes}"
            chunks = chunks_for(item)
            start_at = offset % len(chunks)
            registered = False
            for k, (sid, msgs) in enumerate(chunks[start_at:] + chunks[:start_at]):
                if self.stop.is_set() or time.monotonic() >= self.deadline:
                    return
                if self._send_add(index, user, k, sid, msgs) and not registered:
                    registered = True
                    with self.pool_lock:
                        self.pool.append({"user": user, "item": item_index})
            # An item has about 37 exchanges, too few to fill a level: continue with the next
            # item as a new user.
            item_index = (item_index + 1) % len(self.items)
            passes += 1

    def _send_add(self, index: int, user: str, k: int, sid: str, msgs: list[dict]) -> bool:
        body = {"request_id": f"{user}-{k}", "user_id": user, "session_id": sid, "messages": msgs}
        with self.lock:
            self.inflight += 1
        start = time.monotonic() - self.t0
        out = post(self.args.url, self.key, "/add", body, self.args.add_timeout_s)
        record = AddRecord(
            index,
            start,
            time.monotonic() - self.t0,
            out.status,
            out.error_class,
            max(1, len(msgs) // 2),
        )
        with self.lock:
            self.inflight -= 1
            self.adds.append(record)
            if out.error_class:
                self._note_error(out)
        if out.error_class:  # Pause, then move to the next chunk; this one is not retried.
            self.stop.wait(min(out.retry_after or 5.0, 30.0))
        return out.error_class is None

    def _search_worker(self, index: int) -> None:
        rng = random.Random(index)
        while not self.stop.is_set() and time.monotonic() < self.deadline:
            with self.pool_lock:
                target = rng.choice(self.pool) if self.pool else None
            if target is None:  # no user has been written yet
                self.stop.wait(2.0)
                continue
            body = {
                "query": self.items[target["item"]]["question"],
                "user_id": target["user"],
                "top_k": 100,  # the value AML uses
            }
            start = time.monotonic() - self.t0
            out = post(self.args.url, self.key, "/search", body, self.args.search_timeout_s)
            hits = 0
            if out.error_class is None:
                hits = len(json.loads(out.body).get("data", []))
            record = SearchRecord(
                start, time.monotonic() - self.t0, out.status, out.error_class, hits
            )
            with self.lock:
                self.searches.append(record)
                if out.error_class:
                    self._note_error(out)
            if out.error_class:
                self.stop.wait(min(out.retry_after or 5.0, 30.0))

    def _should_abort(self) -> str | None:
        with self.lock:
            done = list(self.adds)
            unauthorized = any(r.status == 401 for r in done) or any(
                r.status == 401 for r in self.searches
            )
        if unauthorized:
            return "401 from server: wrong AML_ADAPTER_KEY"
        failed = sum(r.error_class is not None for r in done)
        if len(done) >= ABORT_MIN_REQUESTS and failed / len(done) > self.args.abort_error_rate:
            return f"{failed}/{len(done)} Adds failed during the level"
        return None

    def run(self) -> dict:
        args = self.args
        self.t0 = time.monotonic()
        self.deadline = self.t0 + args.level_seconds
        threads = [
            threading.Thread(target=self._add_worker, args=(i,), daemon=True)
            for i in range(self.users)
        ]
        threads += [
            threading.Thread(target=self._search_worker, args=(i,), daemon=True)
            for i in range(args.search_workers)
        ]
        for t in threads:
            t.start()
        abort_reason = None
        interrupted = False
        next_print = 60.0
        try:
            while any(t.is_alive() for t in threads):
                time.sleep(1.0)
                elapsed = time.monotonic() - self.t0
                if abort_reason is None and (abort_reason := self._should_abort()):
                    print(f"  aborting level: {abort_reason}; draining in-flight", flush=True)
                    self.stop.set()
                if elapsed >= next_print:
                    next_print += 60.0
                    self._print_progress(elapsed)
        except KeyboardInterrupt:
            interrupted = True
            abort_reason = "interrupted"
            print("  interrupted: no new requests; draining (Ctrl-C again quits)", flush=True)
            self.stop.set()
            for t in threads:
                t.join()
        return self._summarize(time.monotonic() - self.t0, abort_reason, interrupted)

    def _print_progress(self, elapsed: float) -> None:
        with self.lock:
            done, failed = len(self.adds), sum(r.error_class is not None for r in self.adds)
            inflight, searches = self.inflight, len(self.searches)
        print(
            f"  t={elapsed:4.0f}s in-flight {inflight}, Adds done {done} (failed {failed}), "
            f"Searches {searches}",
            flush=True,
        )

    def _summarize(self, wall: float, abort_reason: str | None, interrupted: bool) -> dict:
        args = self.args
        ok = [r for r in self.adds if r.error_class is None]
        durations = [r.end - r.start for r in ok]
        per_exchange = [(r.end - r.start) / r.exchanges for r in ok]
        window = max(1.0, args.level_seconds - args.ramp_s)
        weighted = window_exchanges(ok, args.ramp_s, args.level_seconds)
        finished = sum(r.exchanges for r in ok if r.end <= args.level_seconds)
        search_ok = [r for r in self.searches if r.error_class is None]
        search_lat = [r.end - r.start for r in search_ok]
        add_errors = count_errors(self.adds)
        return {
            "users": self.users,
            "search_workers": args.search_workers,
            "level_seconds": args.level_seconds,
            "wall_s": round(wall),
            "aborted": abort_reason,
            "interrupted": interrupted,
            "exchanges_per_min": round(weighted / window * 60, 1),
            "finished_only_exchanges_per_min": round(finished / args.level_seconds * 60, 1),
            "seconds_per_exchange_p50": percentile(per_exchange, 0.5),
            "seconds_per_exchange_p95": percentile(per_exchange, 0.95),
            "adds_ok": len(ok),
            "adds_total": len(self.adds),
            "exchanges_ok": sum(r.exchanges for r in ok),
            "add_p50_s": percentile(durations, 0.5),
            "add_p95_s": percentile(durations, 0.95),
            "add_errors": add_errors,
            "add_error_rate": round(sum(add_errors.values()) / max(1, len(self.adds)), 4),
            "searches_ok": len(search_ok),
            "searches_total": len(self.searches),
            "searches_per_min": round(len(search_ok) / args.level_seconds * 60, 1),
            "searches_empty": sum(r.hits == 0 for r in search_ok),
            "search_p50_s": percentile(search_lat, 0.5),
            "search_p95_s": percentile(search_lat, 0.95),
            "search_errors": count_errors(self.searches),
            "error_samples": self.samples,
            "add_records": [asdict(r) for r in self.adds],
            "search_records": [asdict(r) for r in self.searches],
        }


def stop_reason(row: dict, best_rate: float, args) -> str | None:
    """Why the ramp should stop after this level, or None to continue."""
    if row["aborted"]:
        return f"level aborted: {row['aborted']}"
    if row["add_error_rate"] > args.max_error_rate:
        return f"Add error rate {row['add_error_rate']:.1%} above {args.max_error_rate:.1%}"
    if row["add_p95_s"] is not None and row["add_p95_s"] > args.max_add_p95_s:
        return f"Add p95 {row['add_p95_s']}s above {args.max_add_p95_s}s"
    if best_rate > 0 and row["exchanges_per_min"] < best_rate * (1 + args.min_gain):
        return (
            f"throughput {row['exchanges_per_min']}/min did not gain {args.min_gain:.0%} "
            f"over best {best_rate}/min"
        )
    return None


def print_row(row: dict) -> None:
    errs = {k: v for k, v in row["add_errors"].items() if v}
    print(
        f"{row['users']:4d} users: {row['exchanges_per_min']:7.1f} exchanges/min, "
        f"{row['seconds_per_exchange_p50']}s/exchange p50, "
        f"Add p50 {row['add_p50_s']}s p95 {row['add_p95_s']}s, "
        f"{row['adds_ok']}/{row['adds_total']} Adds ok, errors {errs or 'none'}, "
        f"wall {row['wall_s']}s",
        flush=True,
    )
    if row["searches_total"]:
        s_errs = {k: v for k, v in row["search_errors"].items() if v}
        print(
            f"      Search: {row['searches_per_min']}/min, p50 {row['search_p50_s']}s "
            f"p95 {row['search_p95_s']}s, {row['searches_ok']}/{row['searches_total']} ok, "
            f"{row['searches_empty']} empty, errors {s_errs or 'none'}",
            flush=True,
        )
    if row["add_errors"]["429_busy"] or row["search_errors"]["429_busy"]:
        print("      429 busy: adapter slots too low for this level, see run sheet", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(prog="aml_loadtest", description=__doc__.split("\n")[0])
    parser.add_argument("--url", required=True, help="adapter address, e.g. http://host:8901")
    parser.add_argument("--check", action="store_true", help="GET /health and read the key only")
    parser.add_argument("--env-file", type=Path, default=root / "deploy/aml/.env")
    parser.add_argument(
        "--data", type=Path, default=root / "evaluation/datasets/longmemeval/shared40.json"
    )
    parser.add_argument("--levels", default="32,64,96,128,192", help="comma-separated user counts")
    parser.add_argument("--level-seconds", type=int, default=MIN_LEVEL_S)
    parser.add_argument(
        "--ramp-s", type=float, default=30.0, help="start-up stagger, excluded from the window"
    )
    parser.add_argument("--settle-s", type=float, default=30.0, help="pause between levels")
    parser.add_argument(
        "--search-workers", type=int, default=0, help="concurrent Search threads; 0 = writes only"
    )
    parser.add_argument(
        "--search-from", type=Path, help="earlier results file whose written_users Search can use"
    )
    parser.add_argument("--prefix", default="load", help="user_id prefix")
    parser.add_argument(
        "--max-error-rate", type=float, default=0.02, help="stop after a level above this"
    )
    parser.add_argument(
        "--abort-error-rate", type=float, default=0.25, help="stop sending mid-level above this"
    )
    parser.add_argument(
        "--min-gain", type=float, default=0.10, help="stop when throughput gains less than this"
    )
    parser.add_argument("--max-add-p95-s", type=float, default=1200.0, help="stop above this")
    parser.add_argument("--add-timeout-s", type=float, default=1800.0, help="AML's request limit")
    parser.add_argument("--search-timeout-s", type=float, default=120.0)
    parser.add_argument("--out", type=Path, help="results JSON; default in evaluation/outputs/")
    args = parser.parse_args(argv)
    args.url = args.url.rstrip("/")
    args.level_list = [int(x) for x in args.levels.split(",")]
    if args.prefix != "load" and not args.prefix.startswith("load-"):
        parser.error("--prefix must be 'load' or start with 'load-'")
    if args.ramp_s >= args.level_seconds:
        parser.error("--ramp-s must be shorter than --level-seconds")
    return args


def write_results(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    key = load_key(args.env_file)
    if not health(args.url):
        print(f"{args.url}/health is not OK: nothing sent", flush=True)
        return 2
    print(f"{args.url}/health OK, adapter key loaded", flush=True)
    if args.check:
        return 0
    if args.level_seconds < MIN_LEVEL_S:
        print(f"WARNING: levels under {MIN_LEVEL_S}s mostly measure in-flight tails", flush=True)

    items = json.loads(args.data.read_text(encoding="utf-8"))
    run_id = uuid.uuid4().hex[:6]
    root = Path(__file__).resolve().parent.parent
    out = args.out or root / f"evaluation/outputs/capacity/server_ramp_{run_id}.json"
    pool: list[dict] = []
    if args.search_from:
        pool.extend(json.loads(args.search_from.read_text(encoding="utf-8"))["written_users"])
    doc: dict[str, Any] = {
        "run_id": run_id,
        "url": args.url,
        "started_utc": datetime.now(UTC).isoformat(),
        "settings": {
            "levels": args.level_list,
            "level_seconds": args.level_seconds,
            "ramp_s": args.ramp_s,
            "search_workers": args.search_workers,
            "user_prefix": f"{args.prefix}-{run_id}-",
        },
        "stop_reason": None,
        "levels": [],
        "written_users": pool,
    }
    print(f"run {run_id}: users {args.prefix}-{run_id}-*, results {out}", flush=True)

    best_rate = 0.0
    for position, users in enumerate(args.level_list):
        print(
            f"level {users} users, {args.search_workers} search workers, "
            f"{args.level_seconds}s",
            flush=True,
        )
        row = LevelRun(args, key, items, run_id, users, pool).run()
        doc["levels"].append(row)
        print_row(row)
        reason = stop_reason(row, best_rate, args)
        best_rate = max(best_rate, row["exchanges_per_min"])
        doc["stop_reason"] = reason
        write_results(out, doc)
        if reason:
            print(f"stopping: {reason}", flush=True)
            break
        if position + 1 < len(args.level_list):
            time.sleep(args.settle_s)

    print(f"wrote {out}", flush=True)
    print(f"best: {best_rate} exchanges/min. Levels:", flush=True)
    for row in doc["levels"]:
        print_row(row)
    return 1 if any(r["aborted"] for r in doc["levels"]) else 0


if __name__ == "__main__":
    sys.exit(main())
