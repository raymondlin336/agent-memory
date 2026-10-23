# 精排分与融合分混合、阈值保留非正分

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-23 |
| 影响范围 | `jiuwen_memory/retrieval/retriever_impl/pipeline_retriever.py`、`jiuwen_memory/config/defaults.py`，docs/specs/S04-retrieval.md |
| 测试基线 | `pytest tests/unit/retrieval/test_threshold.py tests/integration/retrieval/test_pipeline_retriever.py`：全部通过；`tests/unit` 与 `tests/integration/retrieval` 全量与 mem2.0 基线失败集合一致（无新增失败） |
| Refs | — |

## 背景

精排阶段把融合分**整体替换**为 Reranker 分，阈值阶段再只保留正分候选。默认 Reranker 是
`overlap`（词重叠占比，代码注释称其为 cross-encoder 的占位）。在英文语料上二者叠加的问题：

- jieba / whitespace 分词不做词形还原，`complete` 与 `completed` 不重叠；`is`、`?`、`10`
  这类 token 却计入重叠——语义相关但措辞不同的记忆得 0 分，日期里含 `10` 的无关记忆得高分。
- 0 分候选被阈值整体砍掉：即便向量召回把它排在第一，也到不了 top_k，结果数远小于 top_k。

LongMemEval（英文，40 题，`deepseek-v4-flash` 抽取 + bge-small-en-v1.5 向量 + 内存后端，
保持存储不变只重放检索）在出厂配置下：40 题中 11 题注入块不足 top_k=30（最少 1 条），
72 条关键事实仅 55 条进入前 30。

## 决策

在不改出厂默认的前提下新增两个 retriever params：

1. `rerank_fused_weight`（默认 `0.0`）：> 0 时精排分与融合分各按**本批最大正值**归一到 [0, 1]
   后线性混合 `(1-w)·rerank + w·fused`。保留召回/融合阶段的相关性信号，弱精排器不再独自定序。
2. `rerank_keep_non_positive`（默认 `False`）：True 时非正分候选按分（同分保持融合次序）续在
   正分之后；仅当正分段未被绝对/相对阈值截断时才续上，避免绕过显式阈值。

英文场景的推荐配置不是这两个开关，而是换用真正的 cross-encoder：`reranker.default.target: api`
（已支持，Cohere 风格 `/rerank` 端点）指向 `cross-encoder/ms-marco-MiniLM-L-6-v2` 等模型，
并按记忆规模调大 `rerank_max` / `recall_max`。两个新开关用于**无法部署 cross-encoder 时**限制
弱精排器的损害。

## 拒绝的方案

- **直接改默认值**（如默认 `keep_non_positive=True`）：会改变现有中文部署的结果数与次序，需要
  中文基准验证后再议。
- **min-max 归一**：批内最低分映射到 0 会放大小差异；按最大值归一保留「0 分即无信号」的语义。
- **改进 overlap 分词（小写 / 停用词 / 词形还原）**：属于 overlap 实现内部改进，与本特性正交；
  即便改进，词重叠仍弱于向量与 cross-encoder（见验证），留作遗留。

## 验证

单测：`tests/unit/retrieval/test_threshold.py`（非正分保留语义、阈值截断后不续、混合归一）；
集成：`tests/integration/retrieval/test_pipeline_retriever.py`（0 分候选默认被砍 / 保留 / 混合）。

LongMemEval 检索重放（同一份存储，72 条关键事实，top 5 / top 10 / 进入 top 30）：

| 配置 | top 5 | top 10 | top 30 |
|---|---|---|---|
| 出厂（overlap 精排） | 33 | 40 | 55 |
| overlap + `rerank_keep_non_positive` | 33 | 40 | 57 |
| overlap + keep + `rerank_fused_weight=0.5` | 38 | 45 | 61 |
| overlap + keep + `rerank_fused_weight=0.8` | 40 | 51 | 64 |
| 关闭精排（只用 RRF 融合分） | 44 | 53 | 66 |
| api cross-encoder（ms-marco-MiniLM-L-6-v2） | 54 | 59 | 67 |
| api cross-encoder + `rerank_max/recall_max=300` | 54 | 59 | 68 |
| api cross-encoder + cap 300 + `rerank_fused_weight=0.3` | 52 | 57 | 66 |

结论：英文下 overlap 精排在任何权重下都劣于关闭精排；cross-encoder 明显最好，且不宜再混入融合分。
以上数据在 v0.2.0 上测得（同一份存储逐条重放）；默认配置下本特性与 v0.2.0 的 40 题检索结果逐条一致。

## 已知遗留

- 中文语料上两个开关的效果未测。
- `overlap` 精排器的英文分词（小写、停用词、词形还原）未改。
- `rerank_max=60` 对大于 60 条记忆的空间会截断精排候选；是否调整默认值需结合延迟预算评估。
