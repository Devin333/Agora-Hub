# QASPER 检索评测优化交接（2026-10-21）

## 范围与权威状态

继续实现 `qasper-retrieval-evaluation-optimization`，不是替换为一个小型演示评测器。以当前代码、`openspec/changes/qasper-retrieval-evaluation-optimization/tasks.md` 和实际工件为准；文档存在或任务被勾选，不单独证明实现正确。

先阅读：

1. `docs/qasper-retrieval-evaluation-optimization-prd.md`
2. 对应 OpenSpec 的 `proposal.md`、`design.md`、`specs/qasper-retrieval-evaluation-optimization/spec.md`、`tasks.md`
3. `docs/qasper-retrieval-evaluation.md`
4. `openspec/changes/qasper-retrieval-evaluation-optimization/evidence/README.md`

仓库实际路径 `F:/github/NewsRoom`；`F:/github/Agora-Hub` 是指向它的 junction，并非另一份独立工作区。当前分支应通过 `git status --short --branch` 重新确认，不能仅依赖本文的旧 HEAD 声明。

## 保留其他工作

工作区有 PDF parsing、通用 RAG evaluation、Harness、desktop/frontend 等未提交改动。禁止 `git reset --hard`、`git clean -fd`、覆盖其他会话内容或 blanket `git add .`。共享协议是 QASPER 的真实依赖；提交前核对依赖闭包，不能生成依赖未跟踪文件的不可运行提交，也不能把不相关修改混入本变更。

## 已实现的运行面

- `qasper_baseline.py` / `qasper_protocol.py`：历史基线与版本化 manifest/case/mapping/failure/report 契约。
- `qasper_mapping.py` / `qasper_scoring.py`：冻结 gold、parent/child/span/相邻路径、Strict/Equivalent 评分，不根据候选扩大 gold。
- `qasper_experiments.py` / `qasper_retrieval.py`：七个显式 profile、已有 cache、通道 provenance、参数矩阵。
- `qasper_diagnostics.py`：切片、阶段和通道诊断；application 必须真正接入这些输出，不能只有孤立单测。
- `application/qasper_assets.py` / `qasper_evaluation.py`：业务数据重建与评测编排。
- `ports/qasper_evaluation.py` / `infrastructure/research/qasper_evaluation.py`：仅传输 raw bytes/metadata 的只读输入和不可变工件发布。
- `interfaces/composition/qasper_evaluation.py` / `interfaces/cli/commands/qasper_eval.py`：显式 composition 和 `rag-eval qasper` CLI。

本轮从真实 CLI 和数据运行中发现并修复/验证参数类型、final stage、候选截断、宏平均和输入身份等问题。定向 tests 不能替代真实 CLI integration 或完整 smoke；以最终验证输出为准。

## 真实外部数据与缓存

- dataset：`F:/datasets/qasper/train-dev/qasper-dev-v0.3.json`
- cache：`F:/datasets/qasper/cache/qasper_dev_text-embedding-v4_1024.npz`
- 历史 summary：`F:/datasets/qasper/results/qasper_dev_optimized_full.json`
- 本轮外部诊断目录：`F:/datasets/qasper/results/qasper-retrieval-eval-20261021`

冻结 mapping digest：`330fb4722d01da762584e3cf37aba3f0c469ab0eb1290bf158814231d35b6465`。1005 道问题分为 844 valid、101 needs_review、60 not_applicable。253 是 float evidence 条数，163 是 unanswerable annotation 条数，不能与问题分区相加。历史 888、旧重建 881 与新映射 844 的口径不同，不能同比后宣称提升。

## 尚未完成的验收

1. **门禁已完成，按范围提交**：完整 smoke 为 4947 passed、1 skipped、23 deselected，AgentLoop smoke 与 source validation 通过；最终 QASPER/边界 246 passed，隔离必要依赖快照 467 passed，compile 与两个相关 OpenSpec strict validation 均通过。smoke 在最后一批回归新增前开始，因此新增测试单独复验，不能将旧 collection 当作新增覆盖。只提交 QASPER、必要协议/CLI 依赖和精确共享文件 hunk，不夹带其余解析器/桌面/RAG 改动。
2. **七组完整实验与参数 sweep（6.3）**：六组单查询可用；现有 cache 对 844 道可评测题的 multi-query 共需 1779 个 query key，其中缺少 982 个，全部 844 道题受影响。外部 `cache-audit.json` 和 `missing-multi-query-embeddings.jsonl` 保存清单。不得用原 query 向量替代、跳过变体、合成向量或隐式调用付费 provider。提供同模型完整缓存后在新目录重跑；失败组不能记为成功。
3. **人工审核（6.4）**：外部 `human-review-packet.json` 已准备 40 例（20 高差异、10 对照、10 mapping needs_review），SHA-256 为 `53bfb4e2a1b2ba55a9c63b0c0b4b14214bb0e05802bd98a2c9c2a901784bb410`，含原始标注、冻结路径及 top10 候选正文。全部仍为 `pending_human_review`。需要人类确认 evidence groups、exact child、Equivalent 充分性和阈值，再冻结策略；LLM 不得自批。
4. **文档/工件**：使用说明、PRD、交接和 evidence README 已更新；修复后七组消融位于 `final-ablation`，score 已复核 7035 个逐题结果，report 已生成。小型摘要见 `openspec/changes/qasper-retrieval-evaluation-optimization/evidence/qasper-dev-ablation-20261021.json`。完整参数网格正在外部 `final-sweep` 顺序执行，不把未完成网格记为成功。

修复前的 `qasper-seven-profiles-v2-debug` 和 `ablation` 工件仅用于诊断，不作为最终评分验收，不覆写它们。修复后运行必须另取目录、保存实际代码指纹，并重新确定 score/report 的执行范围。

## 不可越过的边界

CLI 只调 application；storage adapter 不构造业务文档或检索器。评测是 fixture，不冒充 production/Qdrant 的延迟或完整质量。不得写 production index、business memory、active skill，或自行作 production 质量/发布决定。若要切换生产默认策略，另开变更并提供独立验证和回滚依据。

## 复验命令

```powershell
python -m scripts.dev rag-eval qasper --help
openspec validate qasper-retrieval-evaluation-optimization --strict
python -m scripts.dev compile
python -m scripts.dev smoke
```

具体冻结、消融、参数 sweep、score/report 示例见 `docs/qasper-retrieval-evaluation.md`。`--output-dir` 就是后续的 `--run-dir`；`mapping validate` 需要 dataset 路径，不需要 cache。不要根据目录名或一行 `valid=true` 推断所有 profile 已成功；必须检查逐组 planned/valid/needs_review/not_applicable/system_failure 和完整结果覆盖。

## 隔离提交验证说明

提交候选以 `HEAD` 导出为基底，只叠加本次必要文件及共享文件的精确 hunk；467 个测试、compile、真实 mapping validate 在该快照通过，未依赖工作区其余未提交的 `document_fidelity`、PDF parser 或桌面改动。测试快照未纳入无关 legacy comparison adapter 测试及其尚未提交的实现。

本地快照 fetch 曾报告历史对象 `018707f3bb5ae7c3437ceeb9009e035861aad93e` 不可读；随后改用 `git archive HEAD` 和只读 objects alternates 完成隔离验证，未对主仓库执行 repair/reset/clean，也未验证远端 push。不要把本地检查通过等同于完整历史对象健康。
