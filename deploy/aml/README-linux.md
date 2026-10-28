# AML adapter: Linux server quickstart (no Docker)

Linux version of [README.md](README.md): the same services, settings and API, started with a bash script instead of
PowerShell. Runs JiuwenMemory behind the Agent Memory Leaderboard Add/Search contract on one Linux machine:

| Service | Address | What it is |
|---|---|---|
| AML adapter | `http://127.0.0.1:8200` | `scripts/aml_adapter.py` with `deploy/aml/config.yml`: `GET` health, `POST /add`, `POST /search` |
| Rerank server | `http://127.0.0.1:8906/v1` | `scripts/rerank_server.py`: MiniLM cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`) on CPU, Cohere-style `POST /v1/rerank` |
| gpt-4o-mini | `API_BASE` (OpenAI-compatible) | memory extraction during Add |
| text-embedding-v4 | `EMBED_BASE_URL` (DashScope intl) | 1024-dim embeddings |

In-memory mode (`AML_IN_MEMORY=1`) loses all data when the adapter stops; anything that must persist needs the
Redis / Elasticsearch / Milvus backends configured in `deploy/aml/config.yml`.

## Setup

Requirements: Python 3.11+ with the `venv` module, `bash`, `curl`, `git`, outbound HTTPS to the model APIs and (on
first start) to huggingface.co. Where huggingface.co is unreachable, set `HF_ENDPOINT` to a mirror. Ubuntu 24.04 and
Debian 12 ship a suitable Python; Ubuntu 22.04 ships 3.10, which is too old (install `python3.11` and use it below).
Memory: about 600 MB at start (reranker ~400 MB, adapter ~150 MB); in in-memory mode the adapter grows
with the stored memories. On Debian / Ubuntu:

```bash
sudo apt-get install -y python3 python3-venv curl git   # 22.04: python3.11 python3.11-venv
```

Install torch from the CPU-only index first: on Linux the default PyPI build is the CUDA one (several GB of NVIDIA
libraries), which the reranker does not need.

```bash
python3 -m venv .venv            # 22.04: python3.11 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install torch --index-url https://download.pytorch.org/whl/cpu
.venv/bin/python -m pip install -e ".[dev,rerank]" -r evaluation/environment/requirements.txt
cp deploy/aml/.env.example deploy/aml/.env
chmod 600 deploy/aml/.env
```

Settings in `deploy/aml/.env`:

- `API_KEY` (+ `API_BASE`, `MODEL_NAME`): gpt-4o-mini. OpenRouter works: `API_BASE=https://openrouter.ai/api/v1`,
  `MODEL_NAME=openai/gpt-4o-mini`.
- `EMBED_API_KEY`: DashScope key for text-embedding-v4.
- `AML_ADAPTER_KEY`: the key clients send. Empty means `local-dev-key`, accepted only on localhost (see
  [Auth and errors](#auth-and-errors)).
- `RERANK_BASE_URL` (default `http://127.0.0.1:8906/v1`): where the rerank server listens. The `rerank` extra
  installs its dependencies (torch CPU build + transformers). The MiniLM model (~90 MB) downloads from Hugging Face on
  first start and is cached; `HF_HUB_OFFLINE=1` uses only the cache. `RERANK_SERVER_PYTHON` / `RERANK_SERVER_SCRIPT`
  optionally run a different python or server script.

Without `deploy/aml/.env` the script falls back to `evaluation/environment/.env`. Variables already exported in the
shell take precedence over the file.

## Start / stop (one command)

```bash
scripts/start-aml-local.sh
```

Starts the rerank server (unless it already answers) and the adapter, waits until both are healthy, then prints
`ready: ...`. Takes about 15-30 s, mostly loading the reranker model (longer on the first start while it downloads).
Both run in the background and keep running after the shell exits. The rerank server can also run on its own:
`.venv/bin/python scripts/rerank_server.py --port 8906`. Logs, pids and the adapter's request ledger go to
`evaluation/outputs/aml_local/` (`adapter.log.err` has the adapter's log lines).

```bash
scripts/start-aml-local.sh --stop
```

Options: `--port 8200`, `--host 0.0.0.0` (listen beyond localhost), `--env-file <path>`, `--python <path>`.

## Check the services

```bash
curl -s http://127.0.0.1:8200/health      # {"status": "ok"}; any GET path, no key needed
curl -s http://127.0.0.1:8906/v1          # {"status": "ok", "model": "cross-encoder/ms-marco-MiniLM-L-6-v2"}
tail -f evaluation/outputs/aml_local/adapter.log.err   # follow the adapter log
```

## Add

Up to 20 messages per request (that is what AML sends). Returns 200 only after the memories are stored and searchable.
Each user/assistant exchange costs roughly one gpt-4o-mini call, so expect ~5-15 s per exchange.

```bash
curl -s -X POST http://127.0.0.1:8200/add -H "Authorization: Bearer local-dev-key" -H "Content-Type: application/json" \
  -d '{"request_id":"demo-0001","user_id":"demo-user","session_id":"demo-session-1","messages":[{"role":"user","content":"I just adopted a beagle named Biscuit last Saturday.","timestamp":1730000000000},{"role":"assistant","content":"Congratulations on Biscuit! Beagles are great companions.","timestamp":1730000000000}]}'
# {"success": true, "request_id": "demo-0001", "user_id": "demo-user", "session_id": "demo-session-1"}
```

- `timestamp` is optional, Unix milliseconds. It is how relative dates ("last Saturday") get resolved.
- Re-sending the same `request_id` (an AML retry) returns 200 immediately without storing anything twice.

## Search

```bash
curl -s -X POST http://127.0.0.1:8200/search -H "X-Api-Key: local-dev-key" -H "Content-Type: application/json" \
  -d '{"query":"What is the name of my dog?","user_id":"demo-user","top_k":5}'
```

```json
{"data": [{"id": "4f101797-...", "content": "[2024-10-27] On 2024-10-20, the user adopted a beagle named Biscuit.",
           "score": 0.00099, "created_at": "2024-10-27T03:33:20Z"}]}
```

- Results are in rank order, never more than `top_k` (AML uses 100). Search takes about 1 s.
- `user_id` is the only isolation boundary: another `user_id` gets `{"data": []}`.
- `score` is the MiniLM sigmoid score; only its order matters.
- `content` starts with the conversation date; set `AML_DATE_PREFIX=0` to drop it.

## Auth and errors

Auth: `Authorization: Bearer <key>`, `Authorization: Token <key>` or `X-Api-Key: <key>`; all three are always
accepted, and the key is compared in constant time.

On localhost any key works, including `local-dev-key`. On any other address (`--host 0.0.0.0`, a LAN or public IP,
`--host` in `run-aml.sh`) the adapter refuses to start unless `AML_ADAPTER_KEY` is at least 32 characters and not a
development key; `AML_ALLOW_NO_AUTH` has no effect there. Generate one and keep it only in the env file:

```bash
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))"
```

The AML access request asks for the authentication scheme and this key (the "Memory System Key"); the platform
stores it encrypted. Declare one scheme, e.g. `Authorization: Bearer <key>`, and keep the key out of URLs, logs and
commits. Changing it means updating the env file, restarting the adapter, and updating the key bound in AML.

Errors are JSON `{"error": "..."}`:

| Status | When | AML retries? |
|---|---|---|
| 400 | body is not valid JSON | no |
| 401 | missing / wrong key | no |
| 404 | path other than `/add`, `/search` | no |
| 409 | the same Add (`user_id` + `request_id`) is still running | Add: yes |
| 422 | missing or wrong field, e.g. `{"error": "user_id must be a non-empty string"}` | no |
| 429 | too many Adds (8) or Searches (32) at once; has `Retry-After` | yes |
| 500 / 503 | engine or model service failure; 503 also after 25 min in one Add (the retry resumes) | yes |

## Running on a server

The start script detaches both processes, which is enough for testing. For a long-running service that restarts on
failure and on boot, run the two processes under systemd instead (adjust `User` and paths):

```ini
# /etc/systemd/system/aml-rerank.service
[Unit]
Description=AML MiniLM rerank server
After=network-online.target

[Service]
User=aml
WorkingDirectory=/opt/agent-memory
# /var/lib/aml, created and owned by User; holds the model cache (the user may have no home directory).
StateDirectory=aml
Environment=HF_HOME=/var/lib/aml/hf
ExecStart=/opt/agent-memory/.venv/bin/python scripts/rerank_server.py --port 8906
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/aml-adapter.service
[Unit]
Description=AML Add/Search adapter
After=aml-rerank.service
Requires=aml-rerank.service

[Service]
User=aml
WorkingDirectory=/opt/agent-memory
StateDirectory=aml
EnvironmentFile=/opt/agent-memory/deploy/aml/.env
Environment=PYTHONPATH=/opt/agent-memory PYTHONIOENCODING=utf-8
Environment=AML_STATE_DB=/var/lib/aml/aml_adapter_state.db
# In-memory stores, as with the start script; remove to use the Redis / Elasticsearch / Milvus backends.
Environment=AML_IN_MEMORY=1
ExecStart=/opt/agent-memory/.venv/bin/python scripts/aml_adapter.py --config deploy/aml/config.yml --host 127.0.0.1 --port 8200
Restart=on-failure
# SIGTERM shuts the adapter down cleanly (engine and ledger closed).
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now aml-rerank aml-adapter
journalctl -u aml-adapter -f
```

`EnvironmentFile` takes plain `KEY=value` lines, which is the format of `deploy/aml/.env`. Values from that file win
over the unit's `Environment=` lines, so leave `AML_STATE_DB` and `AML_IN_MEMORY` unset in `.env` (as in
`.env.example`) or set them there instead. `StateDirectory=aml` creates `/var/lib/aml` for the request ledger and model cache: keep it on
persistent disk, since deleting the ledger makes AML's retried Adds write twice. Create the service user and give it
read access to the checkout first, e.g. `sudo useradd --system --no-create-home aml`.

AML only connects to public HTTPS endpoints. Keep the adapter on `127.0.0.1` and put a TLS reverse proxy in front of
it. The proxy must allow requests to run for up to 30 minutes (AML's per-request limit) and must not buffer or reject
large bodies; with nginx, for example:

```nginx
location / {
    proxy_pass http://127.0.0.1:8200;
    proxy_read_timeout 1800s;
    proxy_send_timeout 1800s;
    client_max_body_size 64m;
}
```

If the adapter itself listens on a public address instead, `AML_ADAPTER_KEY` must be a strong key (see above).

## Test one full LongMemEval question end to end

`scripts/aml_e2e_one.py` plays the AML platform for one question: it sends the question's sessions to `/add` in
20-message chunks, calls `/search` with `top_k` 100, then answers and judges with gpt-4o-mini at 30/10/20/50 memories,
using the benchmark harness's LongMemEval prompts. With the stack running:

```bash
.venv/bin/python scripts/aml_e2e_one.py --data evaluation/datasets/longmemeval/longmemeval_mini.json --question-id 89527b6b
```

`longmemeval_mini.json` ships with the repo (one question, one short session, about a minute). It exercises the whole
pipeline; a single question's score says little about quality (this one is currently answered "not enough
information"). Larger runs need a LongMemEval file in the same format (e.g. `longmemeval_oracle.json` or
`longmemeval_s_cleaned.json` from the LongMemEval release) passed with `--data`; a typical question takes 5-12 min and
several can run in parallel.

- Reads the same env file as the start script; the answer / judge model is `API_BASE` / `MODEL_NAME` / `API_KEY` unless
  `LONGMEMEVAL_CHAT_UPSTREAM_URL` / `LONGMEMEVAL_CHAT_MODEL` / `LONGMEMEVAL_CHAT_API_KEYS` are set.
- `AML_DATE_PREFIX=0` on the adapter matches the harness's context format exactly.
- Output (scores, answer, retrieved memories, timings) goes to `evaluation/outputs/aml_e2e/`.

## Tuning (env, defaults in brackets)

`AML_MAX_ADD_CONCURRENCY` [8], `AML_MAX_SEARCH_CONCURRENCY` [32], `AML_ADD_DEADLINE_S` [1500],
`AML_DATE_PREFIX` [1], `AML_OPTIONS_IN_QUERY` [0], `AML_IN_MEMORY` [1 in the start script],
`AML_LOG_FULL_ERRORS` [0], `embedder_cache_size` [2048, in `deploy/aml/config.yml` globals; 0 disables].

Logs carry IDs, counts, timings and status codes, not message or memory text. Exception messages are masked
(`RuntimeError: *`) while the traceback's files and lines stay; `AML_LOG_FULL_ERRORS=1` shows full messages for local
debugging only. Logs still contain `user_id` / `request_id` values, so they fall under the same data-retention rules
as the stored memories.
Design notes and the full contract mapping: `docs/features/F08-aml-leaderboard-adapter.md`.
