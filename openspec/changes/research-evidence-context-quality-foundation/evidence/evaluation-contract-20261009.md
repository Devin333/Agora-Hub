# 第 2 节：固定评测合同与复用入口

日期：2026-10-09。对应 `tasks.md` 2.1–2.15。本章编码完成后已集中验证，通过后进入第 3 节；本记录不代替第 7 节最终全量与 smoke 验证。

## 既有入口与产物

| 职责 | 复用位置 | 输出 |
| --- | --- | --- |
| 开发命令 | `scripts/dev.py` 的 `test-rag-eval-gate` | 调用既有 Research/RAG CI CLI |
| CLI | `backend/research/rag/cli/run_ci_eval_gate.py` | 退出码、最终 gate 汇总；支持 `--output-dir` |
| CI | `.github/workflows/ci.yml` 的 RAG eval gate | 同一开发命令；没有新增评测流水线 |
| 固定语料 | `ci_eval_gate.py` 的现有 fixture paper 构造函数 | `.newsroom/eval/ci/fixtures/papers/` |
| Golden set | `EvidenceGoldenSetBuilder` 与既有 `EvidenceEvalOptions` | `.newsroom/eval/ci/golden_set.json` |
| 原始证据评测 | `evidence_eval_runner.py`、`paper_evidence_eval.py`、`paper_evaluation_report.py` | `.newsroom/eval/ci/evidence/evidence_regression_report.json` 及 Markdown |
| 原有晋级检查 | `ci_eval_gate.py` | `.newsroom/eval/ci/promotion_gate_report.json` 及 Markdown |
| 候选/输入/Claim 合同扩展 | `candidate_experiment_eval.py`、`quality_foundation_cases.py`，由原 CI gate 调用 | `.newsroom/eval/ci/candidate_experiment_quality_report.json` 及 Markdown |

固定评测仅在测试夹具中使用本地模拟 worker；检索、输入校验、来源核验、Claim resolver/gate 和回退执行复用实际生产实现。生产路径没有新增模拟业务行为、run store、event store 或 promotion service。

静态清点发现 CI 仍引用已移除的 `test-workflow-runtime-contracts` 和 `test-workflow-domain`；本章已对齐现有 `test-graph-runtime-contracts` 和 `test-graph-domain` 开发命令，避免在执行评测前因入口失效而终止。

## 报告与失败合同

- 每例有稳定 `case_id`、案例版本、明确输入、expected/actual 和原因；输入、来源、条件、Claim 资格、worker 控制字段及回退是独立案例。
- 逐例失败及 aggregate 阈值失败均进入现有 CI gate；降低汇总阈值不能抹去关键案例的实际失败。
- 基线和获准候选分别检索固定语料，实际测量 Hit Rate、evidence coverage、required type coverage、source locator coverage、citation accuracy 和 MRR。
- 两侧缺少指标、指标非有限值或发生质量退化均记录明确失败诊断，不用默认满分补齐。
- 质量通过与 `rollout_eligible` 分开。收益不足不会修改默认关闭配置，也不能被报告为已具备部署启用资格。
- 报告记录阈值与案例版本、实际配置、仓库修订以及受测核心文件的逐文件和聚合 SHA-256。未提交实现由文件 checksum 标识，不仅依赖父提交号。

## 验证安排

先完成本章全部代码与测试实现，再集中运行定向 pytest 和既有离线 CI 命令。测试或固定案例失败时修复根因，然后复验本章；本章通过后才进入第 3 节。全量测试与提交前 smoke 在第 7 节执行。

实际结果（2026-10-09）：

- 第 2 节的 evaluator、固定案例、证据评测、报告/CLI，以及资格决策受影响 domain/application/gate 回归共 111 项通过，日志：`.newsroom/quality-foundation/chapter-2-recheck.log`，JUnit：`chapter-2-recheck.xml`。
- 既有 `python -m scripts.dev test-rag-eval-gate` 连续执行两次均通过，54 个固定案例全部通过。两次 candidate report SHA-256 均为 `eb83708a9dd9e4c387cc53ea1e14b83b41a697192f2cc128da37d5cb61a3cc4e`，比较记录：`.newsroom/quality-foundation/chapter-2-reproducibility.json`。
- 实现版本 `research-retrieval-candidate-v1`，候选固定集 `research-candidate-cases-v2`，输入/Claim 固定集 `research-quality-foundation-cases-v2`，阈值版本 `research-candidate-thresholds-v2`。
- case pass、constraint preservation、fallback accuracy、control-field rejection、input integrity、claim qualification 阈值均为 `1.0`；maximum regression 为 `0.0`，minimum rollout Hit Rate delta 为 `0.01`。
- 六项成对指标（Hit Rate、evidence/type/locator coverage、citation accuracy、MRR）基线与候选均为 `1.0`，delta 为 `0.0`。质量通过，`rollout_eligible=False`，原因 `rollout_gain_threshold_not_met`；不会自动启用试验。
- 首轮测试发现不可发布的资格决策仍生成资格引用。已修复 `ClaimEligibilityDecision`：只有 supported/eligible 生成资格引用，其他决策资格为 `None`；序列化重新核对派生资格，并且固定 CI 同时检查资格存在与 eligibility 一致。该失败已通过上述集中复验。

本地执行时设置 `NEWSROOM_GIT_REVISION` 为受测 HEAD；CI 使用 `GITHUB_SHA`。工作树修改由以下 source checksum 精确标识，`worktree_dirty=null` 表示评测器未自行查询 Git 状态。
| 受测身份 | SHA-256 / 值 |
| --- | --- |
| Parent HEAD | `3f86249c355aab369f2e8590dc9772b1618fde3b` |
| Source aggregate | `11e8af3fc1b1ab812386cbbcb0a67f5354277c2018b39edfa0396a4f478eede8` |
| Case aggregate | `a5d9430320758b132a0995eaee49e7cfd29757d400f67cf237b89de5f29a25e0` |
| Configuration | `6b7538301d37a41ed6f4b034c6394a44e830a10eb931417dfffb8b39e64879e1` |
| Fixture inputs | `876b807e14de19bc4cda146351e75476efae55394a62128fe4e229ed8e4885f2` |

本章已通过的是离线合同与既有 CI 评测；生产部署启用仍受显式配置、来源范围及收益门槛约束。第 7 节会按最终代码重新生成完整验证记录。
