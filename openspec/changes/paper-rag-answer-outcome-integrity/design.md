## Context

论文问答的生产链路是 `PaperRAGSession` → `BoundedRAGSessionController`（plan → 检索 → 证据门禁 → context pack → `PaperAnswerWorker` → `RAGAnswerGate`）。live 评测复用这条链路，底层换成内存 chunk store。

根因定位用的是离线复现：用 golden corpus snapshot 加假 LLM 跑 business question bank。

1. 第 1 轮检索为空。`_execute_search` 总是把 `run_id` 写进检索过滤条件，而评测为每题生成新 `run_id`，语料 chunk 上没有这个 `run_id`。生产环境里语料由同一个 Graph run 解析和索引，所以不受影响。把 `run_id` 统一绑定后，30 道单篇题中有 24 道能走到生成。
2. 剩下 6 道题第 1 轮拿到了证据，但证据类型不满足要求。这时会重新规划，而 `DeterministicRAGPlanner` 不读 `executed_queries`，生成的查询与第 1 轮完全相同，被 `rag_query_dedup` 拦下后 session 中止。
3. `_answer_text_from_live_payload` 在 session 未成功时合成一句拒答文本，于是系统故障被计为"拒答"。

生成侧，`_repair_generated_answer` 和 worker 中的 `_looks_like_context_abstention`、`_negative_presence_relevance_gap` 用关键词和句式启发式改写或判定回答。评测侧又有三套各不相同的 abstain marker 列表。

## Goals / Non-Goals

**Goals**

- 每道题的结果只能来自系统实际发生的事件，即模型回答、模型拒答、证据不足、门禁拒绝或系统失败之一，任何一层都不能改写或伪造。
- 拒答判定只有一个来源：模型按协议给出的显式声明。
- 评测报告的每个比率都带分母；没有分母时报告 N/A。
- 生产与评测走同一条回答路径。

**Non-Goals**

- 不调整检索质量、证据类型判定或 coverage gate 的严格程度。这些在阶段 1 用真实数据测出来之后再定。
- 不做 claim 级 NLI judge、QASPER 端到端评测或 token 成本报表，这些属于阶段 1。
- 不新增 answer gate 失败后的重试循环。

## Decisions

### D1 拒答用固定前缀协议，而不是 JSON 字段

生产路由 `deepseek-v4-flash` 的 `structured_output_capability.mode` 是 `none`，也不允许 JSON 回退。如果使用 `output_schema`，每次问答都会触发 `provider_schema_ineligible`。

所以协议定为：模型输出要么是带 `[n]` 引用的回答，要么整段只有一行 `INSUFFICIENT_EVIDENCE: <原因>`。解析是确定性的：

- 整段 strip 后匹配 `^INSUFFICIENT_EVIDENCE:\s*(.+)$` 且只有一行，判为拒答，冒号后的文本作为原因。
- 输出中别处出现该标记，判为 `malformed_output`，即模型没有遵守协议。
- 空输出，判为 `empty_output`。

这种协议不依赖任何供应商能力。模型如果用含糊措辞拒答而没写前缀，就按回答处理，评测中判错，这反映的是模型的真实行为。

备选方案是先验证供应商的 JSON 模式，或把生产换成支持 schema 的模型。这两条要么受限于供应商能力，要么会改变桌面端的模型和成本，所以没有采用。

### D2 新端口 `PaperAnswerModelPort`

`backend/research/ports/paper_answer_model.py` 定义以下类型：

- `PaperAnswerModelRequest(system_prompt, user_prompt, execution_identity, max_transport_attempts)`
- `PaperAnswerModelResponse(text, usage: TokenUsage, model: str)`
- `PaperAnswerModelError(code, message, retryable)`，其中 `code ∈ {provider_unavailable, timeout, empty_output, configuration}`
- `PaperAnswerModelPort.complete(request) -> PaperAnswerModelResponse`（async）

适配器 `OpenAICompatiblePaperAnswerModel` 放在 `backend/research/application/paper_answer_model.py`，包装 `OpenAICompatibleClient`，负责映射 `LLMProviderError`、`LLMConfigurationError` 和超时。所有构建 `AnswerGenerator` 的地方都改用这个适配器：composition、`paper_rag_factory`、`llm_client.build_unity_paper_answer_model`、评测脚本。这样就消除了三份重复的 `llm_call` 闭包，也去掉了用签名探测 `max_transport_attempts` 的做法。usage 会随响应返回，阶段 1 统计成本时直接使用。

### D3 `AnswerGenerator` 只做三件事

1. 组装上下文。`AnswerContextAssembler` 不变，`required_context_ids` 只由 oracle 轨道传入。
2. 构建提示。Research 自己定义系统指令，内容包括：只依据编号段落作答、用 `[n]` 引用、检索正文是不可信数据且不得当作指令执行、证据不足时只输出拒答行。现有的表格/公式/图像题型提示保留。
3. 调用模型并解析协议。

`GeneratedAnswer` 新增字段：

- `abstained`、`abstention_reason`
- `cited_context_indexes`：合法的 1-based 编号
- `invalid_citation_markers`：越界编号的原文，例如 `"[9]"`
- `model_usage`

回答文本原样保留，不做任何改写。模型调用失败或协议违规时抛 `PaperAnswerModelError`，不产生回答。

### D4 worker 只做投影，判定交给 gate

`PaperAnswerWorker` 的处理方式：

- 删除关键词拒答判定和 negative-presence 相关性判定。
- 下面三类系统或合同问题抛 `AnswerWorkerError(code, message)`，不再伪装成拒答：
  - context pack 无法投影为 paper chunk；
  - 生成器不支持个人记忆；
  - 个人记忆回执不一致。
- `PaperAnswerModelError` 转换为 `AnswerWorkerError(code="model_<code>")`。
- 模型拒答时，返回 `abstained=True` 的候选，`metadata.abstention_source="model"`，并带上原因。
- 模型给出回答时，合法编号映射为 evidence id，越界编号和无法映射到 verified evidence 的引用写入候选新字段 `unresolved_citations`。是否放行由 gate 决定。

`AnswerWorkerError` 定义在 `framework/harness/rag/answer_worker.py`，与 `AnswerWorkerPort` 同处，作为端口合同的一部分。

### D5 gate 与 session 的终态语义

- `RAGAnswerGate._citation_integrity`：`unresolved_citations` 非空就失败。缺少引用的回答本来就会因 claim 没有 evidence 而在 `rag_answer_claim_coverage` 失败；缺少 locator 会在 `rag_answer_span_citation_integrity` 失败。这些都不需要 worker 重复判断。
- `_run_generation_phase` 捕获 `AnswerWorkerError`，写入 `rag_answer_generation_failed` 和 `rag_halted` 事件，决策为 `FAILED`。其他异常照常抛出，不吞掉程序错误。
- answer gate 失败且没有补充轮时，决策为 `HALTED`（原来是 `ABSTAIN`），事件为 `rag_halted`。这与 context pack gate 失败时的处理一致。
- `_handle_plan_failure` 中，如果失败的 gate 只有 `rag_query_dedup`，并且重新规划预算已用完，决策为 `INSUFFICIENT_EVIDENCE`，意思是查询已穷尽、证据仍不足。scope、allowlist、budget 等违规仍然记为 `HALTED`。

### D6 规划器的确定性查询变体

`DeterministicRAGPlanner` 按顺序生成以下候选，取第一个 `normalize_query` 后不在 `executed_queries` 中的：

1. 问题 + 缺失证据类型 + 未支持 claim（现有组合）
2. 问题 + 未支持 claim
3. 问题本身
4. 问题 + 目标实体（`target_entities`）

全部都已执行过时，返回第 1 个候选，交给 dedup gate 判定，再由 D5 记为 `INSUFFICIENT_EVIDENCE`。memory recall 用同一个查询。

### D7 评测结果分类 `AnswerOutcome`

`framework/rag/evaluation/answer_metrics.py` 新增 `AnswerOutcome(StrEnum)`，取值为 `answered`、`abstained`、`insufficient_evidence`、`halted`、`failed`。

- `AnswerMetricCase.outcome` 是必填字段，删除 `looks_like_abstention` 和 `DEFAULT_ABSTAIN_MARKERS`。
- refusal 指 `abstained` 和 `insufficient_evidence`；system failure 指 `halted` 和 `failed`。

打分规则：

- system failure 的所有质量指标为 `None`，`answer_success=False`，`failure_reason=system_failure`，不计入拒答准确率。
- 期望拒答的题：refusal 判为正确，answered 判为错误（`abstention_wrong`）。
- 期望回答的题：refusal 记为 `abstained_over_conservative`，事实和引用指标为 `None`，上下文覆盖仍然计算。

结果汇总新增：`outcome_counts`、`system_failure_rate`、`over_abstention_rate`（分母为期望回答且未系统失败的题）、`abstention_accuracy`（分母为期望拒答且未系统失败的题）。

所有平均值在没有可用值时返回 `None`，报告格式为 `value (n=k)` 或 `N/A (n=0)`。`MetricValue` 不接受 `None`，scorecard 适配器跳过值为 `None` 的指标并在 metadata 中记录。阈值检查本来就会把 `None` 报告为 unavailable。

### D8 live 评测与基准套件

- `_build_in_memory_live_answer_ask` 在整次运行中使用同一个 `run_id`，索引前把它写入每个 chunk 的 metadata。`session_id` 仍然按题生成。
- 生成模型由外层注入，`run_evidence_eval_core` 接收 `answer_model`，CLI 和脚本层用 `build_unity_paper_answer_model` 构建。
- payload 到 sample 的映射只依据 session 状态得出 outcome，不合成任何文本。
- `BenchmarkSuiteConfig.generation_track` 取值 `end_to_end` 或 `oracle_context`，默认 `end_to_end`。只有 `oracle_context` 会调用 `_hydrate_retrieval_with_evidence_pack` 并传入 `required_context_ids`。轨道名写入报告 metadata。
- 引用 ID 直接取 `GeneratedAnswer.cited_context_indexes`，不再从文本二次解析。

## Risks / Trade-offs

- [桌面端部分表格题、公式题回答变差，因为不再拼接原文摘录] → 这是用户已接受的代价。阶段 1 会测出真实水平，再决定从提示或检索层面改进。
- [模型不遵守拒答前缀] → 不做补救，按回答判定。阶段 1 统计 `malformed_output` 的比例，如果偏高再改提示。
- [gate 拒绝由 ABSTAINED 改为 HALTED，前端显示随之变化] → `bounded_document_rag` 已经把 HALTED、FAILED 映射为失败状态，前端已有失败展示。
- [评测历史数字不可比] → 新旧口径不同，报告里注明 outcome 分类版本，不与旧数字做同比。

## Migration Plan

先修规划器和 session 终态，再修生成链路，最后修评测。每一步都跑定向测试，提交前跑 `python -m scripts.dev smoke`。

回滚方式是还原这几个提交。不涉及数据迁移，也不修改任何持久化 schema。


## Follow-up: D9 业务题库的结构性重复片段等价（2026-10-23）

用户已确认父子重复片段可视为等价引用，并删除 `single_007`。该题要求原文未提供的任务名称，不能作为应回答题继续计分。本次只修正业务题库标注和评测口径，不调整生产检索、上下文组装、生成或 Harness 门禁。

业务题库加载器可接收 corpus snapshot 的 chunks。以 `parent_chunk_id` 为边，只有同一论文、正文非空且按空白归一后完全相同的父子片段才建立双向等价关系；闭包须确定性、抗环。正文不同的父子、同正文但无亲缘关系的片段、跨论文片段均不自动等价。未知 gold ID 不扩大，无 corpus 时保留原有精确匹配行为。

严格 gold ID 保持不变，语料派生的替代 ID 仅写入 `equivalent_gold_chunk_ids`。已有 scorer 分别计算 strict 与 equivalent coverage，主指标使用已有 equivalent 口径；运行器先加载 snapshot，再将同一 snapshot 的 chunks 传给题库加载器。等价集合不得依据模型回答、引用或检索命中反推，亦不得注入生产上下文。

删除单题后业务题库为 89 行，41 道可评分题（单篇 29、拒答 12），48 道草稿仍显式跳过。既有真实运行保持原样，若重算必须标注为离线重评分，不能宣称是重新调用模型的结果。
