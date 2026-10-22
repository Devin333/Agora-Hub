## 1. 规划器与 session 终态

- [x] 1.1 `DeterministicRAGPlanner` 按 D6 依据 `executed_queries` 选择首个未执行的查询变体
- [x] 1.2 `_handle_plan_failure`：只有 dedup 失败且无重新规划预算时，决策为 `INSUFFICIENT_EVIDENCE`；其他 plan gate 失败仍为 `HALTED`
- [x] 1.3 新增 `AnswerWorkerError`；`_run_generation_phase` 捕获后记为 `FAILED`，并写入 `rag_answer_generation_failed` 事件
- [x] 1.4 answer gate 失败的终态由 `ABSTAINED` 改为 `HALTED`
- [x] 1.5 `GroundedAnswerCandidate.unresolved_citations`，以及对应的 `RAGAnswerGate` 引用完整性检查
- [x] 1.6 规划器、session、gate、replay 的单测

## 2. 回答生成链路

- [x] 2.1 新增 `PaperAnswerModelPort`、请求/响应/错误类型
- [x] 2.2 新增 `OpenAICompatiblePaperAnswerModel` 适配器和 `build_unity_paper_answer_model`
- [x] 2.3 `AnswerGenerator`：Research 系统指令（含不可信数据声明）、拒答前缀协议解析、合法与越界引用编号、usage；删除 `_repair_generated_answer` 及其专用辅助函数
- [x] 2.4 `PaperAnswerWorker`：删除关键词拒答和 negative-presence 判定；系统问题抛 `AnswerWorkerError`；越界引用写入 `unresolved_citations`
- [x] 2.5 composition、`paper_rag_factory`、数据脚本改用回答模型端口
- [x] 2.6 生成器、worker、个人记忆、composition 的单测

## 3. 评测侧

- [x] 3.1 `AnswerOutcome`；`AnswerMetricCase.outcome` 改为必填；删除关键词拒答判定
- [x] 3.2 `score_answer_case` 按 D7 打分；新增失败原因 `system_failure`、`abstained_over_conservative`、`abstention_wrong`
- [x] 3.3 `EvidenceAnswerSample.outcome`；平均值返回 `None`；新增 `outcome_counts`、`system_failure_rate`、`over_abstention_rate`；报告显示分母
- [x] 3.4 scorecard 适配器和报告 dict 处理 `None`
- [x] 3.5 live 评测：统一 `run_id` 并绑定语料；注入回答模型；按 session 状态映射 outcome，不合成文本
- [x] 3.6 基准套件 `generation_track`，gold 注入只在 `oracle_context` 轨道；引用改用 `cited_context_indexes`
- [x] 3.7 评测侧单测，以及离线复现验证（golden corpus 加假模型，确认不再全部中止）

## 4. 验证与交付

- [x] 4.1 `openspec validate paper-rag-answer-outcome-integrity --strict`
- [x] 4.2 `python -m scripts.dev smoke`
- [ ] 4.3 提交
