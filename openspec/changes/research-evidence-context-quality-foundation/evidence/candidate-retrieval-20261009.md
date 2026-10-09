# 第 5 节：默认关闭的候选检索试验

开始日期：2026-10-09；验证更新：2026-10-10。对应 `tasks.md` 5.1–5.14。本章全部实现和测试代码完成后集中检查，以下结果按实际复验更新。

## 实现与验收边界

| 任务 | 实现位置及真实业务边界 |
| --- | --- |
| 5.1–5.2 | `rag/retrieval/candidate_experiment.py`、`application/paper_rag_session.py`：版本化严格配置、嵌套不可变、默认关闭与零 worker 调用，保留原始确定性计划 |
| 5.3 | `framework/harness/rag/planner.py`、Research adapters、`application/llm_client.py`、`infrastructure/research/candidate_worker.py`：worker/candidate/RAG 查询预算、实际 provider deadline 和物理请求次数上限 |
| 5.4–5.6 | `evaluate_retrieval_candidate`：逐字保留原 query，受控完整扩展词、实体/时间/来源/语言/版本/metric/否定及显式约束；Unicode/CJK 与精确词边界 |
| 5.7–5.8 | operation/corpus/source/scope/tool/memory 边界；递归拒绝原始 JSON 和 typed candidate 中的控制字段，LLM adapter 不得丢掉顶层越权后接受候选 |
| 5.9–5.10 | 所有失败选择预先计算的 baseline，拒绝 worker 伪造 `candidate_fallback`；provider timeout 按稳定类型/code 分类，validator 异常和未知原因不得泄漏原错误文本 |
| 5.11–5.12 | session policy → planner request → step metadata → `retrieval_port` → `QueryPlanner` → `RetrievalPlan.to_dict()`：实际配置和血缘可序列化；真实 controller 回归证明原计划实际执行 |
| 5.13–5.14 | 复用固定 54 案例与冻结阈值，成对实际检索证明质量与收益，默认开关不得由 worker 或评测结果自动修改 |

明确声明的非法配置不能静默变成空约束。可信 session scope 为唯一来源范围；候选缺少 scope 时从该 scope 填入，候选试图改写时拒绝。候选提出额外 source reading、memory access、tool 或超预算查询时，原始计划可执行的请求仍执行原计划。

超时预算沿 `WorkerRAGPlanner → ResearchRAGPlanWorker → StructuredResearchCandidateWorker/LLMResearchRAGPlanCandidateWorker → LLMRequest → OpenAICompatibleClient` 到真实 transport。同步桥接未新增计时线程或脱离控制的后台请求。Provider 的 `LLMProviderError(error_type="timeout")` 映射为稳定 `worker_timeout`，普通网络/服务失败映射为 `worker_failed`。

## 固定对照结果

执行既有 CLI：

```powershell
python -m backend.research.rag.cli.run_ci_eval_gate --output-dir .newsroom/quality-foundation/chapter-5-fixed-eval
```

- 退出码 0，固定 54 个案例全部通过。
- case pass、input integrity、claim qualification、constraint preservation、fallback accuracy、control-field rejection 六项合同指标均为 1.0。
- 成对 Hit Rate、evidence coverage、required type coverage、source locator coverage、citation accuracy、MRR 的 baseline 和 candidate 都为 1.0，实际 delta 都为 0.0。
- `rollout_eligible=False`，原因为 `hit_rate_gain_below_threshold`；没有达到冻结的 0.01 绝对 Hit Rate 增量，候选仍保持默认关闭。
- 首轮报告追踪 48 个实际核心文件；最终本章报告追踪 55 个实际核心文件的逐文件及聚合 checksum，包括 provider transport、TaskPlan/subagent 执行、durable transcript 和 atomic storage。
- 报告与日志位于 `.newsroom/quality-foundation/chapter-5-fixed-eval/` 和 `chapter-5-fixed-eval.log`，第 7 节将在最终代码冻结后再次重跑。

## 集中测试结果

首轮集中检查为 **1,215 通过、3 失败、3 跳过、5 条既有依赖弃用警告，330.04 秒**。失败为 typed 顶层控制字段在领域构造时已提前拒绝、controller fixture 把 corpus scope 当成 source scope，以及默认 gate inventory 错误包含 opt-in gate。前两项按真实边界调整夹具及断言；第三项修复生产 inventory，区分 default、quality 和 registry available。没有放松校验、删除默认 gate 断言或改变 opt-in gate。根因修复后的受影响集中复验为 **218 通过、5 条既有警告，186.16 秒**。复验覆盖 candidate、全部 Research graph gate、默认 fake runtime、opt-in 成功/缺证、生产 composition recorded transport、固定案例及 CI；日志/XML 为 `.newsroom/quality-foundation/chapter-5-recheck.*`。本章通过，全量测试与 smoke 保留到第 7 节。
最终本章 CLI 输出位于 `.newsroom/quality-foundation/chapter-5-final-eval/`，退出码 0、54 案例通过、六项合同率均为 1.0、六项成对 delta 均为 0、`rollout_eligible=False`。已补齐 default/quality gate inventory 和导出入口的核心源码 checksum，并由 CI 单元测试逐文件核对实际 worktree bytes。

首轮三项跳过均为既有 Windows symlink 权限不足（`WinError 1314`）；五条警告来自既有 FastAPI/Starlette 弃用接口，没有新增 skip 或放宽任何断言。