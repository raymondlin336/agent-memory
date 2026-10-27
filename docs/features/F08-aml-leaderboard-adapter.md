# F08 — AML 排行榜 Add/Search 适配层

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-27 |
| 影响范围 | `scripts/aml_adapter.py`、`scripts/run-aml.sh`、`deploy/aml/config.yml`、`deploy/aml/.env.example`、`tests/unit/scripts/test_aml_adapter.py`；不改 `jiuwen_memory/` 内核 |
| 测试基线 | `tests/unit/scripts/test_aml_adapter.py` 38 项全绿（含一项走真实内存引擎的授权链路）；`ruff check` 通过。未对真实 Redis / ES / Milvus / LLM 做端到端联调 |
| Refs | 无（issue 号待确认） |

## 背景

Agent Memory Leaderboard（AML）Cycle 2 开源赛道要求参赛方托管 HTTP 端点：`POST /add`
（写入后须已持久化且立即可检索才返回 200）与 `POST /search`（按相关性返回至多 `top_k`
条），`user_id` 是唯一隔离边界，失败按状态码分类重试。适配层把该协议桥接到进程内的
`MemoryAPI`。

首版适配脚本协议部分齐全（重试账本、409 进行中、鉴权），但与 aml-benchmark 实测的引擎
装配脱节：用手工构造的 `AuthContext` 会被 mem2.0 授权拒绝；默认加载 LoCoMo 配置（精排关、
`recall_max` 200）；逐条消息单独抽取；幂等账本仅在内存。本特性把适配层对齐到基准实测的
写入 / 检索口径，并补齐协议鲁棒性。

## 决策

1. **授权**：进程启动时 `build_dev_authenticator()`，每次调用 `internal_context(authenticator)`，
   与 `evaluation/longmemeval/harness.py` 一致。认证边界是适配层的 `AML_ADAPTER_KEY`；用户隔离
   靠每次显式传入的 `Scope(org="aml", user=user_id)`。启动时用该上下文做一次只读自检，授权或
   存储连不通立即失败，而不是等到第一个请求。
2. **写入口径复用评测适配器**：直接调用 `LongMemEvalDataset` 的 dialogue_turn 分组
   （user+assistant 合并为一个轮次，孤立消息单条，超过 4096 字符按句回退并带前文）与
   `_seed_metadata`（`infer=true`、`retain_source=false`、`observation_date`、`session_date`…）。
   这样 AML 写入形态与基准测过的完全一致，而不是另写一套。代价：依赖评测适配器的私有方法，
   由测试守护其接口。
3. **no-source 抽取**：进程生命周期内启用 `no_source_extraction()`（评测同款
   `retain_source=false`）；`AML_NO_SOURCE=0` 可关。
4. **时间戳**：AML `timestamp`（毫秒）→ 无时区 UTC `datetime` 作 `occurred_at`，并格式化为评测的
   `session_date` 格式写入 metadata；缺失的消息沿用相邻消息的时间戳，全缺则交给引擎默认。
   用无时区 datetime 是因为评测传入的全是无时区值，引擎对有时区值的比较行为未验证。
5. **幂等**：sqlite 账本（`AML_STATE_DB`）按 `(user_id, request_id)` 记录已写入的 group 数，
   每个 group 写完提交一次；重试从断点续写，已完成直接回 200。进行中的同一请求回 409。
   同一 user 的 Add 串行（保持 chunk 顺序）。
6. **同 `request_id` 内容变化 → 视为新请求（进度归零并记 warning）**，而不是 409/422。
7. **配置**：`deploy/aml/config.yml` 是从 aml-benchmark 调优版 `evaluation/longmemeval/config.yml`
   **整份内联**的派生配置（精排开、MiniLM、`recall_max`/`rerank_max` 300、`rerank_fused_weight` 0、
   1024 维），与基准仅有第 8、9 条所述差异。不用 `extends`：本分支基线的 `config_loader` 尚无
   `extends` 支持，且调优版基准配置还没提交，继承会静默退回旧值（精排关、`recall_max` 200）。
8. **ES 全文库 `refresh: wait_for`**：全文库默认 `refresh=false`，刚写入的文档约 1 秒后才可搜，
   关键词召回路会漏掉；这违反 AML「Add 返回即可检索」。向量库 Milvus 默认 Strong，Redis 即时。
9. **AML 专用存储名**：ES 索引 `aml_*`、Milvus 集合 `aml_*`，与评测数据隔离；Milvus 默认指向
   服务端 `http://127.0.0.1:19530` 而非 Lite 文件。
10. **检索**：L2 披露；`created_at` 取 `t_message`（评测 `context_dates` 同口径），内容默认加
    `[YYYY-MM-DD]` 前缀（`AML_DATE_PREFIX=0` 可关）；去重、丢弃空 id / 空内容、`score` 非有限值时省略、
    结果硬截断到 `top_k`。`options`（选择题选项）默认不拼进查询（`AML_OPTIONS_IN_QUERY=1` 开启）。
11. **状态码**：字段 / 类型问题 422、非法 JSON 400（均不重试）；引擎 `ValidationError` 422；
    限流 429；引擎授权失败 500 并记 error 日志（不吞成 503）；其余 503（可重试）。此前所有
    `ValueError/TypeError/KeyError` 一律 400，会把引擎内部故障误判为“合约错误”而不被重试。
12. **并发**：Add 与 Search 分开信号量（默认 8 / 32），避免长 Add 占满槽位把 Search 挤成 429；
    槽位满回 429 + `Retry-After`。
13. **鉴权**：`Authorization: <任意 scheme> <key>`（Token / Bearer）或 `X-Api-Key`，任一匹配；
    `AML_ADAPTER_KEY` 未设置默认拒绝启动（`AML_ALLOW_NO_AUTH=1` 仅限本地调试）。健康检查任意路径 GET 免鉴权。
14. **HTTP**：支持 chunked 请求体、64 MiB 上限、socket 读写 120 秒超时（处理耗时不限）；
    客户端提前断开时写响应失败只记日志，写入已记账，AML 重试命中账本回 200。

## 拒绝的方案

- **整块（20 条消息）合成一次抽取**：与基准无对照数据，且单次输入过长易降低抽取质量；改为沿用基准已验证的
  dialogue_turn 粒度。若以后要换，应先在基准里做对照。
- **继续用手工 `AuthContext(actor=scope)`**：mem2.0 授权按凭据类型注册表校验，会被拒绝。
- **用 Redis / 引擎自身做幂等**：引擎无按外部 `request_id` 去重的能力；加到内核超出本特性范围。sqlite 账本零额外依赖。
- **同 `request_id` 不同内容回 409 / 422**：409 会被 AML 无限重试，422 不重试会静默丢数据；按新请求处理更安全。
- **每次 Add 后显式刷新 ES 索引**：需要触达 ES 客户端私有属性；`refresh: wait_for` 是配置级、无侵入的等价做法。

## 验证

- 单测（含对 `deploy/aml/config.yml` 关键参数的断言）：协议形状与回显、dialogue_turn 分组、时间戳映射、超长按句回退、重试断点续写（含账本重开）、
  同 ID 新内容、按用户隔离 ID、409、字段校验 422、search 去重 / 截断 / 空值 / 日期前缀 / NaN 分数、
  鉴权三种头、状态码映射、本机 HTTP 端到端（假引擎）、真实内存引擎下自检 + Add + Search（证明授权链路可用）。

## 已知遗留

- **未联调真实后端**：Redis / ES / Milvus / gpt-4o-mini / text-embedding-v4 / MiniLM rerank 服务的端到端
  与延迟、`wait_for` 带来的写入开销均未实测。
- **账本与引擎写入之间有极小窗口**：某 group 已写入引擎、账本尚未提交时进程崩溃，重试会重复写该 group。
  账本文件丢失则全部重试都会重复写入。
- **`user_id` 字符集**：`Scope.user` 对特殊字符 / 超长 id 的限制未验证，非法值会得到 422。
- **引擎多线程安全**：依据评测 harness 已按 scope 并发运行，但未对同一进程内高并发 Add + Search 压测。
- **配置需手工同步**：`deploy/aml/config.yml` 内联自 aml-benchmark 工作树里尚未提交的调优版
  `evaluation/longmemeval/config.yml`（取自 2026-10-27 的快照）。基准配置再调整时须同步检索段；
  `config_loader` 的 `extends` 支持合入后可改回继承以消除重复。
- **模型约束**：gpt-4o-mini / text-embedding-v4 只由环境变量决定，未在代码里强校验。
