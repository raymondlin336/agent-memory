# F11 — 英文相对时间检索：英文规则与陈述时间判定

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-26 |
| 影响范围 | `jiuwen_memory/retrieval/query_parser_impl/`（time_parse / simple_query_parser）、`jiuwen_memory/retrieval/retriever_impl/`（pipeline_retriever / predicate_builder / unit_reader）、`jiuwen_memory/common/type_def/`（retrieval / retrieval_filter）、`jiuwen_memory/config/defaults.py`，docs/specs/S04-retrieval.md |
| 测试基线 | 全量 `pytest`（Windows）：失败/错误集合与干净 mem2.0 一致（HTTP/鉴权环境类既有失败，差异项单独重跑确认为偶发），零新增 |
| Refs | F07-absolute-date-recall（issue #252，中文绝对日期两侧共用规则） |

## 背景

事件时间窗是检索层的时间能力，但在英文场景下不生效（mem2.0 默认配置下经 MemoryAPI add → search 实测）：

1. `time_parse` 只有中文规则（相对词 + F07 绝对日期 / ISO 日期）：`yesterday`、`last month`、
   `10 days ago` 不产生时间窗，检索结果与不带时间词相同。
2. 没有事件时间的记忆对任何窗口都放行，而它被说出的时间（`t_message`）从不参与判定；
   抽取侧的事件时间也不总是事件本身：「截至 5/25 的三个月里看了 5 部电影」被记成
   `t_event=2/25`（区间起点），单看 `t_event` 会把「过去三个月」的提问判成窗外。

## 决策

全部开关默认保持出厂行为。

- **英文规则**（`query_parser.params.time_rules_en`，默认关）：中文规则（含 F07 绝对日期）未命中
  后再试。「N 单位前」以该点为中心给容差窗（≤ 跨度 1/4，至少 1 天，上限 day 2 / week 4 /
  month 15 / year 60 天）；`last/past N 单位`、`yesterday`、`day before yesterday`、
  `last <weekday>`、`last/past week|month|year`（日历与滚动取并集）、`this week|month|year`、
  `in <Month> [YYYY]`。「习惯」类表述（`on Saturday mornings`）与「多久以前 / 哪个先发生」类
  问题不产生窗。
- **判定时间**（`retriever.params.time_window_dates`）：`event`（默认，仅 t_event；缺失或 naive
  放行，与出厂一致）| `event_or_message`（t_event 或 t_message 任一落窗）。后者无法用单字段索引
  谓词表达，窗不下推，由召回后真源复核判定；复核成为唯一判定点，naive 时间按 UTC 归一（与索引
  投影 `epoch_ms` 同口径）而不放行，否则窗对 LLM 抽取的 naive `t_event` 形同虚设。
  `ParsedQuery.time_window_dates` 由 Retriever 写入，`is_retrieval_candidate` 的各复核点
  （CompositeDomainStore / KeywordRecaller / Expander / 上卷）读同一字段，不各自传参。

## 拒绝的方案

- **调用级「提问时刻」锚点**（`extensions["query_time"]`）：只在回放历史对话 / 离线评测时有用，
  线上相对时间以真实当前时间为准即可，不纳入。
- **默认开启英文规则**：会改变中英混合部署的现有召回，需中文基准回归后再议。
- **复用 `schema_temporal/time_extractor.py`**：其英文覆盖面不同（有英文绝对日期，无
  `N days ago` / `last <weekday>` / `in <Month>`），且只服务 Schema TemporalEntity 视图；两处
  规则合并留待后续。
- **给索引增加 t_message 投影以支持下推**：涉及 Milvus/ES/pgvector 映射变更，收益只在大规模空间
  的召回预算上；当前先走复核。
- **LLM 解析时间窗 / 窗内前移（不排除）**：本特性只做确定性规则与硬过滤；二者在离线回放中
  无可测收益，不纳入。
- **`event` 模式也把 naive 按 UTC 判定**：更一致，但改变出厂复核行为（既有用例
  `test_event_window_lenient_when_naive_t_event`），不在本特性范围。

## 验证

- 单测：`test_time_parse.py`（英文规则窗口、非约束问句不产生窗、中文规则优先）、
  `test_query_parser.py`（英文规则按当前时间解析、默认关闭）、`test_unit_reader.py` /
  `test_predicate_builder.py`（event_or_message 复核与 naive 归一、不下推）；集成：
  `test_time_window_modes.py`（event / event_or_message / 非法取值）。
- LongMemEval 检索重放（40 题，事实 top10 命中数；评测侧把题目日期作为当前时间注入）：
  出厂 40 → 英文规则 + `event` 40 → 英文规则 + `event_or_message` 41；配合 cross-encoder
  精排 59 → 60。

## 已知遗留

- 英文规则不覆盖 `last weekend`、`since New Year`、节日；写入侧不解析英文绝对日期
  （`on March 5`，F07 `first_absolute_date` 只认中文/ISO 形态）。
- 「多久以前做了 X」类问题需要的是记忆上的日期而非窗口，本特性不改善此类问题。
- `event_or_message` 不下推：大规模空间下窗外候选会占用召回预算。
