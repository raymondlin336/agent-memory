# AML server deployment (Linux, Docker Compose)

Everything needed to serve the Agent Memory Leaderboard Add/Search API from one Linux server, with durable storage.
One script runs it all; no service needs to be installed or wired up by hand.

| Container | Role | Reachable from |
|---|---|---|
| `adapter` | `scripts/aml_adapter.py`: `GET` health, `POST /add`, `POST /search` | host `127.0.0.1:8200` (or HTTPS via `caddy`) |
| `rerank` | `scripts/rerank_server.py`: MiniLM cross-encoder, model baked into the image | compose network only |
| `redis` | memories and metadata (source of truth), append-only file on disk | compose network only |
| `elasticsearch` | keyword search (BM25), 3 indexes | compose network only |
| `milvus` | vector search, 3 collections; embedded mode (its own etcd, files on local disk) | compose network only |
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

**B. An existing reverse proxy or load balancer** (domain, TLS and firewall managed separately). Give whoever runs it
the requirements below.

### Requirements for the domain / firewall / proxy setup

| Item | Requirement |
|---|---|
| Upstream target | `http://127.0.0.1:8200` when the proxy runs on this server. If it runs on another machine, set `AML_BIND_ADDRESS` in `deploy/aml/.env` to this server's private IP, run `./aml-server.sh up`, and allow port 8200 only from the proxy |
| Public URL | `https://<domain>` with a publicly trusted certificate; AML rejects plain HTTP and private, loopback or link-local addresses |
| Paths and methods | `POST /add`, `POST /search`, and `GET` on any path for the health check (`/health`); no other paths are needed |
| Timeouts | read and send timeouts of **at least 30 minutes** (one Add may take that long; AML allows 30 minutes per request); connect timeout can stay short |
| Request body | at least **64 MB** (an Add carries up to 20 messages; the adapter accepts up to 64 MB) |
| Headers | pass `Authorization` and `X-Api-Key` through unchanged; the adapter checks the key itself, so the proxy must not add its own authentication |
| Status codes | pass the adapter's status codes and `Retry-After` through unchanged (AML retries 408/409/425/429/5xx); no caching, no rewriting of error responses |
| Health check | `GET https://<domain>/health` returns 200 without a key, which is also what AML's health probe uses |

nginx example (on this server):

```nginx
location / {
    proxy_pass http://127.0.0.1:8200;
    proxy_read_timeout 1800s;
    proxy_send_timeout 1800s;
    client_max_body_size 64m;
}
```

Once it is in place, `curl -s https://<domain>/health` from any machine should print `{"status": "ok"}`.

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
boot). Container logs are capped at 5 files of 50 MB per container, so a long run cannot fill the disk with logs. Data is in the Docker volumes `aml_redis-data`, `aml_es-data`, `aml_milvus-data` and `aml_adapter-data` (the
request ledger). Deleting the ledger volume alone would make AML's retried Adds store everything twice, so remove
data only with `wipe`.

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
| `milvus` unhealthy for the first minutes | normal; it can need 1-3 minutes on a slow disk. `logs milvus` shows progress |
| image build fails downloading packages or the model | the network blocks PyPI or Hugging Face: set `PIP_INDEX_URL` / `HF_ENDPOINT` to mirrors in `deploy/aml/.env` |
| `docker compose up` cannot pull images | Docker Hub is blocked or rate-limited: configure a registry mirror in `/etc/docker/daemon.json` |
| `check`: Add fails with 503 | model call failed: check `API_KEY` / `EMBED_API_KEY`, then `./aml-server.sh logs adapter` |
| `check`: 401 with your key | `AML_ADAPTER_KEY` changed after the containers started: run `up` again |
| HTTPS check fails | DNS not pointing at the server yet, or ports 80/443 closed; `./aml-server.sh logs caddy` |
| `502` / `504` from the public URL during long Adds | the proxy's read/send timeout is under 30 minutes (see the proxy requirements in step 6) |
| port 8200 already in use | set `AML_PORT` to another port in `deploy/aml/.env` |

When asking for help, include the output of `./aml-server.sh status` and `./aml-server.sh logs adapter` (logs contain
IDs and timings, never message text or keys).

## Status of this setup

Run end to end under Docker (Docker Desktop on WSL2, engine 29.9.2, Compose 5.6.1): `up` built and started all
five containers, `check` passed (health, 401, 404, Add, Search, user isolation) on the real Redis / Elasticsearch /
Milvus stores, and `durability` passed (a memory written before restarting every container was found afterwards, and
the retried Add was answered from the ledger). Recreating the containers (as `up` does after a settings or code
change) kept the data and the log limits applied. Not yet exercised: a Linux host, the `caddy` HTTPS profile, a
proxy in front, and a full leaderboard-sized load.
