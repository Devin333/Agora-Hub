# 论文问答端到端评测：阶段 0 交接（2026-10-22）

## 1. 先读什么、以哪份为准

本变更**没有单独的 PRD**。需求、设计和验收标准都在 OpenSpec change 里，以它为准：

| 文件 | 作用 |
|---|---|
| `openspec/changes/paper-rag-answer-outcome-integrity/proposal.md` | 为什么改、改什么（相当于 PRD） |
| `openspec/changes/paper-rag-answer-outcome-integrity/design.md` | 根因、决策 D1–D8、风险 |
| `openspec/changes/paper-rag-answer-outcome-integrity/specs/paper-rag-answer-outcome-integrity/spec.md` | 验收要求和场景（MUST） |
| `openspec/changes/paper-rag-answer-outcome-integrity/tasks.md` | 任务清单，`[x]` 表示已完成 |

注意：`.gitignore` 第 232 行忽略了 `openspec/`，但仓库里已有的 change 都是强制加入的。提交时必须用 `git add -f openspec/changes/paper-rag-answer-outcome-integrity`。

项目规则见 `CLAUDE.md` 和 `AGENTS.md`：回复和文档用中文，代码用英文；要交付最终版本，不做最小实现；出问题要从根因修；不要写高耦合代码；每次改完代码要提交，提交前 `python -m scripts.dev smoke` 必须通过。

## 2. 总体评测计划（阶段 0–3）与用户决策

这套评测按 RAG 主流做法分四个阶段，本文只交接阶段 0。

- **阶段 0（本变更）**：修好测量本身。把系统失败和拒答分开，去掉回答修复层，拒答改为显式协议，指标没有分母时报 N/A，gold 证据只允许在 oracle 轨道中注入。
- **阶段 1**：在 QASPER dev 上按题型分层抽约 300 题，走生产路径 `PaperRAGSession` 做端到端评测，一次产出以下指标：
  - 回答：QASPER Answer-F1，加 LLM judge 判正确性和错误事实。
  - 拒答：用不可回答题算正确拒答率和过度拒答率。
  - 引用支持率：拆成 claim 后由 judge 判定，人工抽 50 例校准 judge。
  - 上下文漏斗：检索命中 → 进入最终上下文 → 被引用，并记录截断。
  - 效率：完成率、P50/P95、tokens、单题成本。
- **阶段 2**：QASPER 加一个"生产 pipeline"检索 profile；为租户 fail-open 写确定性测试；加 20–30 条论文正文提示注入用例。
- **阶段 3**：解析评测冻结，只收编 scorer 编排和失败率。跨来源报告（ReportBench）暂不做。

用户已确认的决策：

1. 跨来源研究报告不是近期功能，ReportBench runner 不做。
2. 阶段 1 可以真实调用 LLM，几百到一千多次调用可以接受。
3. 回答修复层整体去掉。生产路径只保留两件事：校验引用编号是否合法，以及模型显式声明拒答。LLM 失败就记为失败。
4. 拒答用固定前缀协议，不用 JSON 字段（原因见 design D1：生产模型 `deepseek-v4-flash` 的 structured output mode 是 `none`）。
5. 模型（2026-10-22 更新）：
   - 桌面生产已切换到 `research-desktop-answer` 路由 / `gpt-5.6-sol`（见 `openspec/changes/research-desktop-answer-model-gpt-5-6-sol`）。
   - 评测生成：`build_unity_paper_answer_model` 与生产共用 `backend/research/application/llm_selectors.py` 的选择键，按 `NEWS_RESEARCH_LLM_*` → `NEWS_LLM_*` → `OPENAI_*` 解析，当前即 `gpt-5.6-sol`。
   - judge：仍走 `OPENAI_*`（当前 `gpt-5.6-terra`）。用户要求 judge 用 6.1-sol，**等用户确认网关上的准确模型 ID 后**再加独立的 judge 选择键，不要猜测模型名。
   - `.env` 含密钥，不要回显其中的值。

6. 评测规则（2026-10-22 用户确认）：
   - 期望拒答的是否题，"明确否定 + 引用"也算拒答正确，记为 `abstention_basis = cited_negation`。
   - 网关会对生命科学类问题直接内容拒答，用户确认这一点无法规避。这类题会表现为 `halted`（系统失败）；阶段 1 需要把生物医学论文单独分层统计。

## 3. 分支与提交状态

- 本地 `main` 已快进到 `8b9df6c0`，内容是其他会话的解析、评测协议、桌面端改动，合入前 smoke 通过 5211 passed。这次合并**没有推送**。
- 当前分支：`codex/e2e-answer-evaluation`，从 `main` 切出。
- 阶段 0 的改动**全部未提交**，原因是第 3 组改到一半，工作区目前无法 import（见第 5 节）。
- 接手后先完成第 3 组，跑 smoke，然后**按文件明确地 `git add`**，不要用 `git add .`。`.tmp/` 下的复现脚本不要提交。

## 4. 已完成的部分（第 1、2 组，定向测试全部通过）

### 第 1 组：规划器与 session 终态（framework/harness/rag）

- `planner.py`：新增公开函数 `deterministic_query_text()`，内部是 `_query_variants` 加 `_first_unexecuted_query`。按 D6 的顺序取第一个未执行过的查询变体。`FakeRAGPlanner`（`fake.py`）复用同一个函数，并删除了它自己的重复实现。
- `session.py`：
  - `_handle_plan_failure` 中，如果只有 `rag_query_dedup` 失败且没有重新规划预算，决策为 `INSUFFICIENT_EVIDENCE`。对应 helper 是 `_only_query_dedup_failed`。
  - `_run_generation_phase` 捕获 `AnswerWorkerError`，写入 `rag_answer_generation_failed` 和 `rag_halted` 两个事件，状态记为 `FAILED`，`answer=None`。其他异常继续抛出。
  - answer gate 失败时，状态由 `ABSTAINED` 改为 `HALTED`，事件为 `rag_halted`。
- `answer_worker.py`：新增 `AnswerWorkerError(code, message, retryable)`。
- `models.py`：`GroundedAnswerCandidate.unresolved_citations` 新字段，`to_dict` 中也会输出。
- `answer_gate.py`：`_citation_integrity` 在 `unresolved_citations` 非空时判失败。
- `metrics.py`：`RAGSessionMetrics.answer_generation_failures`。
- 测试：`tests/framework/harness` 全部通过，2578 passed。新增和修改的用例在 `test_rag_generation_phase.py`、`test_rag_plan_gates.py`、`test_rag_session_controller.py` 三个文件中。

### 第 2 组：回答生成链路

- 新端口 `backend/research/ports/paper_answer_model.py`，包含 `PaperAnswerModelPort`（async `complete`）、`PaperAnswerModelRequest/Response`、`PaperAnswerModelError(code)`。code 的取值为 `provider_unavailable | timeout | configuration | empty_output | malformed_output`。
- 适配器 `backend/research/application/paper_answer_model.py` 中的 `OpenAICompatiblePaperAnswerModel`：
  - 发送 system 和 user 两条消息；
  - 把 `LLMProviderError` 和 `LLMConfigurationError` 映射为端口错误；
  - 返回 usage。
- `llm_client.py`：抽出 `_build_unity_client()`，新增 `build_unity_paper_answer_model(max_tokens=600)`。`build_unity_llm_call` 保留，judge 和 planner 仍然在用。
- `framework/rag/generation/grounding.py`：新增 `numbered_context_block()` 和 `partition_citation_markers()`（返回 `CitationMarkers(valid_indexes, invalid_markers)`）。原有 `cited_context_indexes()` 改为基于这两个函数实现，行为不变。
- `paper_answer_generator.py`（从 1066 行减到约 650 行）：
  - 删除整个修复层，包括 `_repair_generated_answer`、负向存在题模板、extractive fallback、表格/公式/caption 锚点摘录，以及 `inspect` 签名探测。
  - `AnswerGenerator(model: PaperAnswerModelPort)`，构造时会做 isinstance 校验。
  - 新增 `parse_answer_protocol()`，模块常量为 `ABSTENTION_TOKEN = "INSUFFICIENT_EVIDENCE"`、`ANSWER_PROTOCOL_VERSION`。
  - 新增 `build_answer_system_prompt()`，内容包含不可信数据声明、拒答协议和原有题型提示；新增 `build_answer_user_prompt()`。
  - `GeneratedAnswer` 新增字段 `abstained`、`abstention_reason`、`cited_context_indexes`（1-based）、`invalid_citation_markers`、`model`、`model_usage`，以及属性 `cited_chunk_ids`。
  - 个人记忆相关的安全不变量保持不变：带记忆发送时 `max_transport_attempts=1`；发送前立即用 provider 重新校验；没有 live provider 就拒绝发送。
- `backend/research/rag/adapters/answer_worker.py`（从 676 行减到 392 行）：
  - 删除关键词拒答判定和 negative-presence 判定。
  - 系统问题抛 `AnswerWorkerError`，code 包括 `context_pack_not_projectable`、`personal_memory_unsupported`、`personal_memory_receipt_mismatch`，以及 `model_<code>`。
  - 模型拒答时，`abstained=True`，`metadata.abstention_source="model"`。
  - 越界编号和无法映射的编号写入 `unresolved_citations`，交给 gate 判断。
  - metadata 中保留 `generated_answer_metadata`、`used_personal_memory`（下游 `paper_rag_service.py` 和 `research_service.py` 会读），新增 `model`、`model_usage`。
- 已接入新端口的地方：`interfaces/composition/research.py` 中的 `_configured_paper_answer_worker_factory`，以及 `interfaces/services/paper_rag_factory.py`。
- 测试 fake：`tests/fixtures/paper_answer_model.py` 中的 `FakePaperAnswerModel`。它按顺序返回脚本化的回复，回复可以是文本、错误或 callable，并记录收到的 requests。
- 已通过的测试：`test_generator.py`（24）、`adapters/test_answer_worker.py`（12）、`test_personal_memory_context.py`（17）、`application/test_llm_client.py`（4）。

### 第 3 组已完成部分

- `framework/rag/evaluation/answer_metrics.py`：
  - 新增 `AnswerOutcome(StrEnum)`，属性有 `is_refusal`、`is_system_failure`。
  - `AnswerMetricCase.outcome` 改为必填；非 answered 的 case 带回答文本时直接报错。
  - `score_answer_case` 按 D7 打分。
  - 删除 `looks_like_abstention` 和 `DEFAULT_ABSTAIN_MARKERS`。
- `failure_reason.py`：新增 `ABSTAINED_OVER_CONSERVATIVE`、`ABSTENTION_WRONG`、`SYSTEM_FAILURE`；旧的 `ABSTENTION_EXPECTED` 保留，用于兼容旧的别名。
- `tests/framework/rag/evaluation`：26 passed。

## 5. 当前中断点：工作区无法 import

`backend/research/rag/evaluation/paper_answer_eval.py` 第 10 行仍然在 import 已删除的 `looks_like_abstention`。依赖它的 `evidence_eval_runner`、`paper_benchmark_suite`、`ci_eval_gate`、`interfaces.composition.research`、`interfaces.services.paper_rag_factory` 都会跟着 import 失败。第 3 组剩下的第一步就是修好这里。

## 6. 剩余任务（第 3 组）

### 6.1 `backend/research/rag/evaluation/paper_answer_eval.py`
- 删除以下内容：`_ABSTAIN_MARKERS`、`_looks_like_eval_abstention`、`abstention_failure_reason`，以及对 `looks_like_abstention` 的 import。
- `OVERCONSERVATIVE_ABSTENTION_REASON` 和 `WRONG_ABSTENTION_REASON` 改为指向 `RAGFailureReason.ABSTAINED_OVER_CONSERVATIVE.value` 和 `ABSTENTION_WRONG.value`。`paper_benchmark_suite` 和测试在引用这两个名字。
- `EvidenceAnswerSample` 新增必填字段 `outcome: AnswerOutcome`。dataclass 的字段顺序要求它放在 `answer` 之后、所有带默认值的字段之前，还要补一个"非 answered 的样本不能带回答文本"的校验。
- `_metric_case_from_sample` 传入 `outcome`；`score()` 中删除 `abstain_markers=`；`failure_reason` 直接取 `score.failure_reason`。
- `_average_optional` 改为返回 `float | None`。所有 `*_score()`、`abstention_accuracy()`、`answer_fact_coverage()` 的返回类型都改成 `float | None`。
- 新增 `outcome_counts()`、`system_failure_rate()`（halted 与 failed 合计占总数的比例）、`over_abstention_rate()`（分母为期望回答且非系统失败的题）。
- `report()` 中每一行都要显示分母，格式为 `0.812 (n=27)`；没有可用值时显示 `N/A (n=0)`；另外输出 outcome 分布。

### 6.2 `paper_evaluation_report.py` 和 `adapters/evaluation_scorecard_adapter.py`
- `_answer_result_to_dict` 增加 `outcome_counts`、`system_failure_rate`、`over_abstention_rate`，允许值为 None。
- `_threshold_values` 遇到 None 时不写入值。`issues()` 已经会把缺失值报告为 unavailable，这部分不用改。
- scorecard 适配器：`MetricValue` 不接受 None，所以跳过值为 None 的指标，并把被跳过的名字记到 metadata。

### 6.3 `evidence_eval_runner.py`
- `_build_in_memory_live_answer_ask`：整次运行使用同一个 `run_id`，例如 `paper-rag-answer-eval-run-<suffix>`。索引前用 `chunk.model_copy(update={"metadata": {**chunk.metadata, "run_id": run_id}})` 把它写进每个 chunk。所有题都用这个 run_id 构建 graph identity；`session_id` 仍然按题生成。
- 回答模型改为注入：`run_evidence_eval_core(..., answer_model: PaperAnswerModelPort | None)`。由外层 CLI（`backend/research/rag/cli/run_evidence_eval.py`、`run_live_answer_eval.py`）用 `build_unity_paper_answer_model()` 构建后传入。这一层内部不再自己构建模型。
- 删除 `_answer_text_from_live_payload`。改为用 session 状态映射 outcome：
  - answered → ANSWERED
  - abstained → ABSTAINED
  - insufficient_evidence → INSUFFICIENT_EVIDENCE
  - halted → HALTED
  - failed → FAILED
  - 其他状态，例如 succeeded（说明生成没有启用），抛 `ValueError`
  - 只有 ANSWERED 才取 `answer_text`。
- `_deterministic_answer_sample`：期望拒答的题用 `outcome=ABSTAINED, answer=""`，其余用 `ANSWERED`。
- sample 的 metadata 中记录 `model_usage`（从 `answer_candidate.metadata.model_usage` 取），供阶段 1 统计成本。

### 6.4 `paper_benchmark_suite.py`（以及 `paper_benchmark_matrix.py`）
- `BenchmarkSuiteConfig` 新增 `generation_track: Literal["end_to_end", "oracle_context"] = "end_to_end"`。只有 `oracle_context` 才调用 `_hydrate_retrieval_with_evidence_pack`，并向 `generate` 传入 `required_context_ids`。轨道名写进报告 metadata。CLI `run_benchmark_suite.py` 增加 `--generation-track` 参数。
- `config.answer_llm_call` 和 `_build_answer_llm_call` 改为 `answer_model: PaperAnswerModelPort | None`，默认用 `build_unity_paper_answer_model(max_tokens=700)`。matrix 中第 68 行和第 211 行要同步修改。
- `_generate_answer_samples`：
  - 捕获 `PaperAnswerModelError`，sample 记为 `outcome=FAILED`、`answer=""`，metadata 中写入 `model_error_code`。
  - `abstained` 为真时记为 ABSTAINED。
  - 引用 ID 改用 `answer.cited_chunk_ids`，删除 `_cited_chunk_ids` 这类从文本二次解析的代码。
- `_answer_judge_failure_reasons`（约第 1757 行）：不再调用 `abstention_failure_reason`，改为从 record 中的 outcome 推导；`_write_answer_samples` 要把 outcome 写进 record。
- `_answer_judge_sample_records`：非 ANSWERED 的样本不送 LLM judge。

### 6.5 数据脚本
`data/eval/run_generation_eval.py` 第 54 行和 `run_system_eval.py` 第 56 行，把 `AnswerGenerator(build_unity_llm_call(...))` 改为 `AnswerGenerator(build_unity_paper_answer_model(...))`。judge 继续使用 `build_unity_llm_call`。

### 6.6 测试
- 要更新的测试：`tests/backend/research/rag/test_answer_eval.py`（19 处 `EvidenceAnswerSample(`）、`test_benchmark_suite.py`（3 处，另外第 740–850 行是 abstention reason 相关的断言）、`test_evaluation_report.py`（4 处），以及 `test_ci_eval_gate.py` 和 `test_routing.py`。
- 新增一个集成测试：用 `data/eval/golden_corpus_snapshot.json` 和 `FakePaperAnswerModel` 跑 `_build_in_memory_live_answer_ask`。要断言 30 道单篇题都不再出现 `rag_query_dedup` 导致的 halted，且每题检索都非空。复现方法见第 7 节，可以直接改写成测试。
- 用 `git grep` 找出 `EvidenceAnswerSample(`、`AnswerGenerator(`、`answer_llm_call` 还没改完的调用点，确保全部改完。

### 6.7 交付
`openspec validate paper-rag-answer-outcome-integrity --strict` → `python -m scripts.dev smoke`（大约 35 分钟）→ 更新 tasks.md 的勾选状态 → 按文件提交，提交信息用中文。

## 7. 根因证据与复现方法

复现脚本放在 `.tmp/`，没有提交，内容如下：

- `.tmp/repro_live_halt.py`：
  1. 用 `load_golden_corpus_snapshot(Path("data/eval/golden_corpus_snapshot.json"), golden_set_path=Path("data/eval/golden_set.json"))` 加载语料。
  2. 把 `llm_client.build_unity_llm_call` 替换为假 LLM，不走网络。
  3. 调用 `_build_in_memory_live_answer_ask(chunks, retrieval_policy="paper_blind_semantic_rag_v1", ...)`。
  4. 用 `data/eval/business_question_bank_v1_draft.jsonl` 中的 `single_001` 构造 `EvidenceQAPair`，执行后打印 transcript events。
- 修改前的结果：第 1 轮 `search_corpus` 返回 `items: []`。原因是过滤条件中带了题目级的随机 `run_id`，而语料 chunk 上没有这个 run_id。第 2 轮查询与第 1 轮完全相同，被 dedup 拦下后 halt。LLM 调用次数为 0。
- 统一 `run_id` 后的结果，对应 `.tmp/repro_runid_fix.py`：
  - 有 gold 的 30 道单篇题中，24 道 answered。
  - 另外 6 道（single_011/012/014/021/029/030）属于"证据类型不符"，触发 replan 后又出现重复查询。
  - 规划器修复后，第 2 轮会换一个新查询，这 6 道题应该以 `insufficient_evidence` 或 answered 结束，**不应再出现 halted**。修完评测侧后需要用复现脚本确认。
- `run_id` 过滤是生产设计：`framework/harness/rag/session.py` 的 `_execute_search` 总会注入 `run_id`，`bounded_document_rag.py` 会把语料绑定到 run。所以要修的是评测器，不能去掉过滤。

## 8. 注意事项

- 不要恢复以下任何启发式：关键词拒答、摘录拼接、模板回答。spec 中有明确的 MUST NOT。
- 不要把 gate 拒绝改回 ABSTAINED，也不要在评测里合成任何文本。
- 搜索请用 `git grep`。仓库下的 `.venv/` 和 `node_modules/` 很大，普通 grep 或 find 会超时。
- 项目里没有 ruff 和 pyflakes，用 `python -m scripts.dev compile` 和测试来验证。
- Explore 类子代理配置的模型不可用；需要子代理时，用 general-purpose 类型。
- `.artifacts/`（5.4G）和根目录的 `node_modules/` 已加入 `.gitignore`，不要提交。
- 业务题库的历史结果在 `.newsroom/eval/business_question_bank_v1_*`，是旧口径，**不能拿来和新结果做同比**。

## 9. 验证命令

```powershell
python -m pytest tests/framework/harness/rag tests/framework/rag tests/backend/research/rag tests/backend/research/application/test_llm_client.py tests/interfaces/services/test_paper_rag_factory.py -q -p no:cacheprovider
openspec validate paper-rag-answer-outcome-integrity --strict
python -m scripts.dev compile
python -m scripts.dev smoke
```
