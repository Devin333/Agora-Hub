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

1. **完整门禁和提交**：必须执行定向 tests、compile、OpenSpec strict validation 及 `python -m scripts.dev smoke`；失败先定位根因，不修改断言掩盖问题。运行中的长门禁要核验真实 handle，不因观察超时重启。
2. **七组完整实验与参数 sweep（6.3）**：六组单查询可用；现有 cache 对 844 道可评测题的 multi-query 共需 1779 个 query key，其中缺少 982 个，全部 844 道题受影响。外部 `cache-audit.json` 和 `missing-multi-query-embeddings.jsonl` 保存清单。不得用原 query 向量替代、跳过变体、合成向量或隐式调用付费 provider。提供同模型完整缓存后在新目录重跑；失败组不能记为成功。
3. **人工审核（6.4）**：高 Strict/Equivalent 差异样本需要人类复核，之后才能冻结 overlap 阈值和 metric version。LLM 自动抽样/解释不是人类审核，不得自批 `pending_human_review`。
4. **最终文档/工件（6.5）**：保留完整 manifest、逐题结果、分母、失败归因和报告；仓库只存小型摘要/哈希，原始数据、大缓存和敏感内容留在仓库外。

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
