## Why

论文问答的端到端评测数字目前不可信，原因是测量本身有错，并非模型表现差：

- live 评测的 42 题全部在生成前中止，但被记成了"拒答"。
- 评测器为每题生成新的 `run_id`，而检索按 `run_id` 隔离，所以第 1 轮检索结果全是空的。
- `DeterministicRAGPlanner` 不读取已执行的查询，重新规划时生成完全相同的查询，被 `rag_query_dedup` 拦下后 session 直接中止。
- 生成侧有一层回答修复启发式：对特定句式返回写死的拒答文本，把表格题、公式题的回答整段换成原文摘录，LLM 报错时拼一个摘录充当回答。
- worker 和评测各自用一套关键词去猜"是不是拒答"。系统失败、引用非法、模型主动拒答被折叠成同一个 `abstained`。
- 没有分母的指标被平均成 0.0；基准套件还会把 gold 证据注入生成上下文。

进入阶段 1（QASPER 端到端评测）之前，必须先让每一题的结果如实反映系统实际发生了什么。

## What Changes

- **BREAKING** 回答生成去掉全部修复启发式。生产路径只保留两件事：
  - 校验 `[n]` 引用编号是否合法；
  - 模型按固定前缀协议显式声明拒答，即整段输出恰好是一行 `INSUFFICIENT_EVIDENCE: <原因>`。
- **BREAKING** 生成模型通过新的 `PaperAnswerModelPort` 调用，失败抛出带类型的 `PaperAnswerModelError`，不再吞掉异常后返回空回答或摘录。
- **BREAKING** `PaperAnswerWorker` 不再用关键词判断拒答。projection 缺失、个人记忆回执不一致等系统问题抛 `AnswerWorkerError`。非法引用写入候选的 `unresolved_citations`，由 `RAGAnswerGate` 判失败。
- **BREAKING** RAG session 的终态按原因区分：
  - 生成异常记为 `FAILED`，并写入 `rag_answer_generation_failed` 事件；
  - answer gate 拒绝记为 `HALTED`，不再记为 `ABSTAINED`；
  - 只有模型主动拒答才是 `ABSTAINED`；
  - 查询穷尽（plan gate 只因重复查询失败）记为 `INSUFFICIENT_EVIDENCE`。
- `DeterministicRAGPlanner` 依据 `executed_queries` 依次尝试确定性的查询变体，选第一个未执行过的。
- 评测引入显式结果分类 `AnswerOutcome`（answered / abstained / insufficient_evidence / halted / failed），删除基于关键词的拒答判定，具体包括：
  - 系统失败不计入拒答准确率；
  - 新增过度拒答率和系统失败率；
  - 没有分母的指标返回 `None`，报告显示 `N/A (n=0)`；
  - live 评测在整次运行中使用同一个 `run_id` 并把它绑定到语料，不再合成拒答文本。
- 基准套件新增 `generation_track`：`end_to_end` 不注入 gold；`oracle_context` 注入 gold，且必须在报告中标明。
- Research 系统提示声明检索正文是不可信数据，不得作为指令执行。

## Capabilities

### New Capabilities

- `paper-rag-answer-outcome-integrity`：论文问答的回答结果分类、拒答协议、失败语义，以及评测侧如实计数。

### Modified Capabilities

- 无已归档的 spec 需要修改。本变更替换的行为来自未归档的 `paper-rag-answer-faithfulness-calibration-v1` 等变更中的修复层实现。

## Impact

- `framework/harness/rag/`：`planner.py`、`session.py`、`answer_gate.py`、`answer_worker.py`、`models.py`
- `framework/rag/evaluation/answer_metrics.py`、`failure_reason.py`
- `backend/research/ports/`：新增 `paper_answer_model.py`
- `backend/research/rag/retrieval/paper_answer_generator.py`、`backend/research/rag/adapters/answer_worker.py`
- `backend/research/application/`：新增结构化回答模型适配器，`llm_client.py` 改为构建该适配器
- `backend/research/rag/evaluation/`：`paper_answer_eval.py`、`evidence_eval_runner.py`、`paper_benchmark_suite.py`
- `interfaces/composition/research.py`、`interfaces/services/paper_rag_factory.py`
- 桌面端用户可见行为：表格题、公式题不再拼接原文摘录；LLM 失败显示为失败，不再伪装成回答或拒答。
