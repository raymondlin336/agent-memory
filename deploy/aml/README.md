# AML adapter: local quickstart (no Docker)

Runs JiuwenMemory behind the Agent Memory Leaderboard Add/Search contract on one Windows machine:

| Service | Address | What it is |
|---|---|---|
| AML adapter | `http://127.0.0.1:8200` | `scripts/aml_adapter.py` with `deploy/aml/config.yml`: `GET` health, `POST /add`, `POST /search` |
| Rerank server | `http://127.0.0.1:8906/v1` | MiniLM cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`), Cohere-style `POST /v1/rerank` |
| gpt-4o-mini | `API_BASE` (OpenAI-compatible) | memory extraction during Add |
| text-embedding-v4 | `EMBED_BASE_URL` (DashScope intl) | 1024-dim embeddings |

In-memory mode (`AML_IN_MEMORY=1`) loses all data when the adapter stops; anything that must persist needs the
Redis / Elasticsearch / Milvus backends configured in `deploy/aml/config.yml`.

## Setup

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -e ".[dev]" -r evaluation/environment/requirements.txt
cp deploy/aml/.env.example deploy/aml/.env
```

Settings in `deploy/aml/.env`:

- `API_KEY` (+ `API_BASE`, `MODEL_NAME`): gpt-4o-mini. OpenRouter works: `API_BASE=https://openrouter.ai/api/v1`,
  `MODEL_NAME=openai/gpt-4o-mini`.
- `EMBED_API_KEY`: DashScope key for text-embedding-v4.
- `AML_ADAPTER_KEY`: the key clients send. The start script uses `local-dev-key` when it is empty.
- `RERANK_SERVER_PYTHON` / `RERANK_SERVER_SCRIPT`: the python (with `sentence-transformers`) and rerank server script
  that serve `RERANK_BASE_URL`, e.g. lme_kit's `embed_serve.py`. Ignored when `RERANK_BASE_URL` already answers.
  The server loads MiniLM from the Hugging Face cache (`HF_HUB_OFFLINE=1`).

Without `deploy/aml/.env` the script falls back to `evaluation/environment/.env`.

## Start / stop (one command)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-aml-local.ps1
```

Starts the rerank server (unless it already answers) and the adapter, waits until both are healthy, then prints
`ready: ...`. Takes about 15-30 s, mostly loading the reranker model. Logs, pids and the adapter's request ledger go to
`evaluation\outputs\aml_local\` (`adapter.log.err` has the adapter's log lines).

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start-aml-local.ps1 -Stop
```

Options: `-Port 8200`, `-BindHost 0.0.0.0` (listen beyond localhost), `-EnvFile <path>`, `-Python <path>`.

## Check the services

```powershell
Invoke-RestMethod http://127.0.0.1:8200/health      # {"status":"ok"}; any GET path, no key needed
Invoke-RestMethod http://127.0.0.1:8906/v1          # rerank server stats (it also serves bge embeddings, unused here)
Get-Content evaluation\outputs\aml_local\adapter.log.err -Tail 20 -Wait   # follow the adapter log
```

## Add

Up to 20 messages per request (that is what AML sends). Returns 200 only after the memories are stored and searchable.
Each user/assistant exchange costs roughly one gpt-4o-mini call, so expect ~5-15 s per exchange.

```powershell
$h = @{ Authorization = "Bearer local-dev-key" }
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
curl -s -X POST http://127.0.0.1:8200/add -H "Authorization: Bearer local-dev-key" -H "Content-Type: application/json" \
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
curl -s -X POST http://127.0.0.1:8200/search -H "X-Api-Key: local-dev-key" -H "Content-Type: application/json" \
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

Auth: `Authorization: Bearer <key>`, `Authorization: Token <key>` or `X-Api-Key: <key>`.
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

Needs `evaluation/datasets/longmemeval/shared40.json` and `LONGMEMEVAL_CHAT_*` settings in the env file (used to answer
and judge, like the benchmark harness):

```bash
AML_ADAPTER_KEY=local-dev-key .venv/Scripts/python.exe scripts/aml_e2e_one.py --question-id 1d4e3b97
```

It sends the question's sessions to `/add` in 20-message chunks, calls `/search` with `top_k` 100, then answers and
judges at 30/10/20/50 memories. Expect about 5-12 min per question; several can run in parallel. Output goes to
`evaluation/outputs/aml_e2e/`.

## Tuning (env, defaults in brackets)

`AML_MAX_ADD_CONCURRENCY` [8], `AML_MAX_SEARCH_CONCURRENCY` [32], `AML_ADD_DEADLINE_S` [1500],
`AML_DATE_PREFIX` [1], `AML_OPTIONS_IN_QUERY` [0], `AML_IN_MEMORY` [1 in the start script].
Design notes and the full contract mapping: `docs/features/F08-aml-leaderboard-adapter.md`.
