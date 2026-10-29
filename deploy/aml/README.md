# AML adapter: local quickstart (no Docker)

Runs JiuwenMemory behind the Agent Memory Leaderboard Add/Search contract on one Windows machine (Linux:
[README-linux.md](README-linux.md)):

| Service | Address | What it is |
|---|---|---|
| AML adapter | `http://127.0.0.1:8200` | `scripts/aml_adapter.py` with `deploy/aml/config.yml`: `GET` health, `POST /add`, `POST /search` |
| Rerank server | `http://127.0.0.1:8906/v1` | `scripts/rerank_server.py`: MiniLM cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`) on CPU, Cohere-style `POST /v1/rerank` |
| gpt-4o-mini | `API_BASE` (OpenAI-compatible) | memory extraction during Add |
| text-embedding-v4 | `EMBED_BASE_URL` (DashScope intl) | 1024-dim embeddings |

In-memory mode (`AML_IN_MEMORY=1`) loses all data when the adapter stops; anything that must persist needs the
Redis / Elasticsearch / Milvus backends configured in `deploy/aml/config.yml`.

## Setup

Requirements: Python 3.11+, Windows PowerShell 5.1+ for the start script, outbound HTTPS to the model APIs and (on
first start) to huggingface.co. On Windows the repo has paths longer than 260 characters: clone into a short directory
or enable long paths (`git config --global core.longpaths true`). Where huggingface.co is unreachable, set
`HF_ENDPOINT` to a mirror.

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev,rerank]" -r evaluation/environment/requirements.txt \
  --extra-index-url https://download.pytorch.org/whl/cpu
cp deploy/aml/.env.example deploy/aml/.env
```

Settings in `deploy/aml/.env`:

- `API_KEY` (+ `API_BASE`, `MODEL_NAME`): gpt-4o-mini. OpenRouter works: `API_BASE=https://openrouter.ai/api/v1`,
  `MODEL_NAME=openai/gpt-4o-mini`.
- `EMBED_API_KEY`: DashScope key for text-embedding-v4.
- `AML_ADAPTER_KEY` (required): the key every Add / Search must send, locally and on a server alike; at least 32
  random characters (see [Auth and errors](#auth-and-errors)). The scripts and examples read it from this file.
- `RERANK_BASE_URL` (default `http://127.0.0.1:8906/v1`): where the rerank server listens. The `rerank` extra
  installs its dependencies (torch CPU build + transformers). The MiniLM model (~90 MB) downloads from Hugging Face on
  first start and is cached; `HF_HUB_OFFLINE=1` uses only the cache. `RERANK_SERVER_PYTHON` / `RERANK_SERVER_SCRIPT`
  optionally run a different python or server script.

Without `deploy/aml/.env` the script falls back to `evaluation/environment/.env`.

## Start / stop (one command)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-aml-local.ps1
```

Starts the rerank server (unless it already answers) and the adapter, waits until both are healthy, then prints
`ready: ...`. Takes about 15-30 s, mostly loading the reranker model (longer on the first start while it downloads).
The rerank server can also run on its own: `python scripts/rerank_server.py --port 8906`. Logs, pids and the adapter's request ledger go to
`evaluation\outputs\aml_local\` (`adapter.log.err` has the adapter's log lines).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-aml-local.ps1 -Stop
```

Options: `-Port 8200`, `-BindHost 0.0.0.0` (listen beyond localhost), `-EnvFile <path>`, `-Python <path>`.

## Check the services

```powershell
Invoke-RestMethod http://127.0.0.1:8200/health      # {"status":"ok"}; any GET path, no key needed
Invoke-RestMethod http://127.0.0.1:8906/v1          # {"status":"ok","model":"cross-encoder/ms-marco-MiniLM-L-6-v2"}
Get-Content evaluation\outputs\aml_local\adapter.log.err -Tail 20 -Wait   # follow the adapter log
```

## Add

Up to 20 messages per request (that is what AML sends). Returns 200 only after the memories are stored and searchable.
Each user/assistant exchange costs roughly one gpt-4o-mini call, so expect ~5-15 s per exchange.

```powershell
$key = ((Select-String -Path deploy\aml\.env -Pattern '^AML_ADAPTER_KEY=').Line -split '=', 2)[1]
$h = @{ Authorization = "Bearer $key" }
$add = @{
  request_id = "demo-0001"; user_id = "demo-user"; session_id = "demo-session-1"
  messages = @(
    @{ role = "user"; content = "I just adopted a beagle named Biscuit last Saturday."; timestamp = 1730000000000 },
    @{ role = "assistant"; content = "Congratulations on Biscuit! Beagles are great companions."; timestamp = 1730000000000 }
  )
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post http://127.0.0.1:8200/add -Headers $h -ContentType "application/json" -Body $add
# {"success":true,"request_id":"demo-0001","user_id":"demo-user","session_id":"demo-session-1"}
```

Same with curl (Git Bash):

```bash
KEY=$(grep '^AML_ADAPTER_KEY=' deploy/aml/.env | cut -d= -f2)
curl -s -X POST http://127.0.0.1:8200/add -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"request_id":"demo-0001","user_id":"demo-user","session_id":"demo-session-1","messages":[{"role":"user","content":"I just adopted a beagle named Biscuit last Saturday.","timestamp":1730000000000},{"role":"assistant","content":"Congratulations on Biscuit! Beagles are great companions.","timestamp":1730000000000}]}'
```

- `timestamp` is optional, Unix milliseconds. It is how relative dates ("last Saturday") get resolved.
- Re-sending the same `request_id` (an AML retry) returns 200 immediately without storing anything twice.

## Search

```powershell
$q = @{ query = "What is the name of my dog?"; user_id = "demo-user"; top_k = 5 } | ConvertTo-Json
Invoke-RestMethod -Method Post http://127.0.0.1:8200/search -Headers $h -ContentType "application/json" -Body $q | ConvertTo-Json -Depth 5
```

```bash
curl -s -X POST http://127.0.0.1:8200/search -H "X-Api-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"query":"What is the name of my dog?","user_id":"demo-user","top_k":5}'
```

```json
{"data": [{"id": "aa98bf22-...", "content": "[2024-10-27] On 2024-10-20, the user adopted a beagle named Biscuit.",
           "score": 0.00099, "created_at": "2024-10-27T03:33:20Z"}]}
```

- Results are in rank order, never more than `top_k` (AML uses 100). Search takes about 1 s.
- `user_id` is the only isolation boundary: another `user_id` gets `{"data": []}`.
- `score` is the MiniLM sigmoid score; only its order matters.
- `content` starts with the conversation date; set `AML_DATE_PREFIX=0` to drop it.

## Auth and errors

Auth: `Authorization: Bearer <key>`, `Authorization: Token <key>` or `X-Api-Key: <key>`; all three are always
accepted, and the key is compared in constant time.

The key is required everywhere, with the same rule on every address and port: the adapter refuses to start unless
`AML_ADAPTER_KEY` is set, at least 32 characters, and not a placeholder such as `local-dev-key`. There is no
key-free mode; only the `GET` health check needs no key.

**One key for everything.** The same `AML_ADAPTER_KEY` value goes in every `deploy/aml/.env` (each developer's
machine and the server) and in the AML access request, so a request that works locally works unchanged against the
server. Get the existing key from whoever holds it (e.g. the team's password manager) rather than generating a new
one; generate a key only when none exists yet:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

Every machine whose `.env` holds the key can write to and read from the live endpoint, so treat those files as
secrets: `deploy/aml/.env` is git-ignored; keep the key out of chat, screenshots, shell history and URLs.

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

## Test one full LongMemEval question end to end

`scripts/aml_e2e_one.py` plays the AML platform for one question: it sends the question's sessions to `/add` in
20-message chunks, calls `/search` with `top_k` 100, then answers and judges with gpt-4o-mini at 30/10/20/50 memories,
using the benchmark harness's LongMemEval prompts. With the stack running:

```bash
.venv/Scripts/python.exe scripts/aml_e2e_one.py --data evaluation/datasets/longmemeval/longmemeval_mini.json --question-id 89527b6b
```

`longmemeval_mini.json` ships with the repo (one question, one short session, about a minute). It exercises the whole
pipeline; a single question's score says little about quality (this one is currently answered "not enough
information"). Larger runs need a
LongMemEval file in the same format (e.g. `longmemeval_oracle.json` or `longmemeval_s_cleaned.json` from the LongMemEval
release) passed with `--data`; a typical question takes 5-12 min and several can run in parallel.

- Reads the same env file as the start script; the answer / judge model is `API_BASE` / `MODEL_NAME` / `API_KEY` unless
  `LONGMEMEVAL_CHAT_UPSTREAM_URL` / `LONGMEMEVAL_CHAT_MODEL` / `LONGMEMEVAL_CHAT_API_KEYS` are set.
- `AML_DATE_PREFIX=0` on the adapter matches the harness's context format exactly.
- Output (scores, answer, retrieved memories, timings) goes to `evaluation/outputs/aml_e2e/`.

## Tuning (env, defaults in brackets)

`AML_MAX_ADD_CONCURRENCY` [8], `AML_MAX_SEARCH_CONCURRENCY` [32], `AML_ADD_DEADLINE_S` [1500],
`AML_DATE_PREFIX` [1], `AML_OPTIONS_IN_QUERY` [0], `AML_IN_MEMORY` [1 in the start script],
`AML_LOG_FULL_ERRORS` [0].

Logs carry IDs, counts, timings and status codes, not message or memory text. Exception messages are masked
(`RuntimeError: *`) while the traceback's files and lines stay; `AML_LOG_FULL_ERRORS=1` shows full messages for local
debugging only. Logs still contain `user_id` / `request_id` values, so they fall under the same data-retention rules
as the stored memories.
Design notes and the full contract mapping: `docs/features/F08-aml-leaderboard-adapter.md`.
