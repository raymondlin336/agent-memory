# AML server deployment (Linux, Docker Compose)

Everything needed to serve the Agent Memory Leaderboard Add/Search API from one Linux server, with durable storage.
One script runs it all; no service needs to be installed or wired up by hand.

| Container | Role | Reachable from |
|---|---|---|
| `adapter` | `scripts/aml_adapter.py`: `GET` health, `POST /add`, `POST /search` | host `127.0.0.1:8200` (or HTTPS via `caddy`) |
| `rerank` | `scripts/rerank_server.py`: MiniLM cross-encoder, model baked into the image | compose network only |
| `redis` | memories and metadata (source of truth), append-only file on disk | compose network only |
| `elasticsearch` | keyword search (BM25), 3 indexes | compose network only |
| `milvus` (+ `etcd`, `minio`) | vector search, 3 collections | compose network only |
| `caddy` (optional) | HTTPS with an automatic certificate, 30-minute request timeouts | ports 80 / 443 |

All data lives in Docker volumes and survives restarts, reboots and image rebuilds. The models (gpt-4o-mini,
text-embedding-v4) are called over the internet with your keys, as in the local setup.

## 1. Prepare the server

Needs: a Linux x86_64 server (e.g. Ubuntu 22.04 / 24.04 or Debian 12; Python does not need to be installed, it runs
inside the containers), 4+ CPU cores, 8 GB RAM minimum (16 GB recommended for a full leaderboard run), 40 GB free
disk, outbound internet.

Install Docker Engine with the compose plugin, and let your user run it:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"      # then log out and back in
docker compose version               # should print a version
```

Elasticsearch needs a kernel setting (kept across reboots):

```bash
echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-elasticsearch.conf
sudo sysctl --system
```

## 2. Get the code

```bash
git clone -b linux-deployment https://github.com/raymondlin336/agent-memory.git
cd agent-memory
```

## 3. Fill in the settings

```bash
cp deploy/aml/.env.example deploy/aml/.env
chmod 600 deploy/aml/.env
openssl rand -hex 32                 # prints a random adapter key; copy it
nano deploy/aml/.env
```

Set these lines (leave the rest as they are; the stack sets store addresses itself):

| Setting | Value |
|---|---|
| `API_KEY` | gpt-4o-mini key. With OpenRouter also set `API_BASE=https://openrouter.ai/api/v1` and `MODEL_NAME=openai/gpt-4o-mini` |
| `EMBED_API_KEY` | DashScope key for text-embedding-v4 |
| `AML_ADAPTER_KEY` | the key printed by `openssl rand -hex 32`; the same key goes into the AML access request |
| `AML_DOMAIN` | leave unset for now (step 6) |

The adapter refuses to start with an empty, short or development key.

## 4. Start

```bash
cd deploy/aml/server
./aml-server.sh up
```

`up` checks Docker, the settings file, memory, disk and `vm.max_map_count`, then builds the two images and starts all
containers. The first run downloads several GB and takes 10-20 minutes; later starts take 1-2 minutes. It prints
`ready: http://127.0.0.1:8200` when the adapter answers. While Elasticsearch and Milvus start, the adapter may restart
a few times on its own; that is expected.

## 5. Verify

```bash
./aml-server.sh check         # health, 401 without key, 404, Add, Search, user isolation
./aml-server.sh durability    # writes a memory, restarts every container, reads it back
```

Each prints `[ OK ]` / `[FAIL]` lines. `check` makes real model calls (a few cents). `durability` proves the point of
this setup: a memory written before a full restart is still found after it, and a retried Add is answered from the
request ledger instead of being stored twice.

Manual requests work the same as locally, with your key:

```bash
KEY=$(grep '^AML_ADAPTER_KEY=' ../.env | cut -d= -f2)
curl -s -X POST http://127.0.0.1:8200/search -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"query":"What is the name of my dog?","user_id":"demo-user","top_k":5}'
```

## 6. Make it public over HTTPS

AML only calls public `https://` URLs. Two ways:

**A. Built-in HTTPS (simplest).** Needs a domain name.

1. Point the domain's DNS `A` record at the server's public IP.
2. Open ports 80 and 443 (cloud firewall / security group, and on the server e.g. `sudo ufw allow 80,443/tcp`).
3. Add `AML_DOMAIN=your.domain.example` to `deploy/aml/.env`.
4. Run `./aml-server.sh up` again, then `./aml-server.sh check` (it also checks `https://your.domain.example/health`).

Caddy gets and renews the certificate itself and allows 30-minute requests.

**B. An existing reverse proxy or load balancer.** Forward to `http://127.0.0.1:8200` on the server, with read/send
timeouts of at least 30 minutes and a body limit of at least 64 MB (nginx example in `deploy/aml/README-linux.md`).

What goes into the AML access request:

| Field | Value |
|---|---|
| Add URL | `https://your.domain.example/add` |
| Search URL | `https://your.domain.example/search` |
| Health | `https://your.domain.example/health` (any `GET` path works, no key) |
| Auth | `Authorization: Bearer <AML_ADAPTER_KEY>` |

## Operating

| Task | Command (from `deploy/aml/server`) |
|---|---|
| State and health of every container | `./aml-server.sh status` |
| Follow logs | `./aml-server.sh logs` or `./aml-server.sh logs adapter` |
| Apply a settings change | edit `../.env`, then `./aml-server.sh up` |
| Update the code | `git pull`, then `./aml-server.sh up` (rebuilds, keeps data) |
| Stop, keep data | `./aml-server.sh down` |
| Delete all stored data | `./aml-server.sh wipe` (asks for confirmation) |

Containers restart on their own after a crash or a reboot (`restart: unless-stopped`; the Docker service starts at
boot). Data is in the Docker volumes `aml_redis-data`, `aml_es-data`, `aml_etcd-data`, `aml_minio-data`,
`aml_milvus-data` and `aml_adapter-data` (the request ledger). Deleting the ledger volume alone would make AML's
retried Adds store everything twice, so remove data only with `wipe`.

Evaluation data may be used only for the run and must be deleted within 30 days after it: `./aml-server.sh wipe`
removes every memory, index and the ledger; container logs go with the containers.

Tuning settings (`AML_MAX_ADD_CONCURRENCY`, `AML_MAX_SEARCH_CONCURRENCY`, `AML_ADD_DEADLINE_S`, `AML_DATE_PREFIX`, ...)
are the same as in the local setup and go in `deploy/aml/.env`; see `deploy/aml/README-linux.md`. For long runs,
`ES_HEAP=2g` gives Elasticsearch more room.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `up` stops at a `[FAIL]` line | fix that item in `deploy/aml/.env` and run `up` again |
| `permission denied ... docker.sock` | your user is not in the `docker` group yet: log out and back in after `usermod` |
| `elasticsearch` keeps restarting, log mentions `max_map_count` | redo the `vm.max_map_count` step |
| `elasticsearch` exits on a small machine / OOM in logs | set `ES_HEAP=512m` in `deploy/aml/.env`, run `up` |
| Elasticsearch stops accepting writes, log mentions `flood stage` / disk watermark | the disk is over ~95% full: free space, then `./aml-server.sh up` |
| `milvus` unhealthy for the first minutes | normal; it needs 1-3 minutes after etcd and MinIO. `logs milvus` shows progress |
| image build fails downloading packages or the model | the network blocks PyPI or Hugging Face: set `PIP_INDEX_URL` / `HF_ENDPOINT` to mirrors in `deploy/aml/.env` |
| `docker compose up` cannot pull images | Docker Hub is blocked or rate-limited: configure a registry mirror in `/etc/docker/daemon.json` |
| `check`: Add fails with 503 | model call failed: check `API_KEY` / `EMBED_API_KEY`, then `./aml-server.sh logs adapter` |
| `check`: 401 with your key | `AML_ADAPTER_KEY` changed after the containers started: run `up` again |
| HTTPS check fails | DNS not pointing at the server yet, or ports 80/443 closed; `./aml-server.sh logs caddy` |
| port 8200 already in use | set `AML_PORT` to another port in `deploy/aml/.env` |

When asking for help, include the output of `./aml-server.sh status` and `./aml-server.sh logs adapter` (logs contain
IDs and timings, never message text or keys).

## Status of this setup

The compose file, images and scripts were written and statically checked without a Docker host available; their first
real run is on the server. The adapter, rerank server and their settings are the same code that passed the local
tests; the new parts are the container wiring and the real stores (Redis / Elasticsearch / Milvus), which the
`check` and `durability` commands exercise end to end.
