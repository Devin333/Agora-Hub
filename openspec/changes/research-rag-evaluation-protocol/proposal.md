## Why

现有 Paper RAG 已有黄金集、检索/回答评分、人工抽检、A/B 比较和 CI，但冻结的人审题库、生产路径、黄金证据辅助实验与固定样例尚未形成可比较的统一协议。需要把用户要求的检索、黄金证据生成、端到端、引用/拒答、成本延迟和多方案评测接入现有机制，避免把合成答案、hash embedding 或 gold 辅助上下文的成绩误报为真实 RAG 效果。

## What Changes

- 建立版本化题库协议：QASPER-inspired 标题/摘要提问与全文标注隔离、来源和审核记录、论文级开发/保留集划分、原文证据锚点及按索引版本生成的 chunk 映射。区分需要共同满足的证据与可替代的充分证据集合。
- 将题库制作与执行分离；正式实验只读取冻结且已审核的数据集与完整语料，不在每次运行中根据待测 chunks 重建问题或 gold。现有 79 题先审计为开发/回归候选，新增未参与调参的论文形成保留集；不宣称现有题已完成复核。
- 在现有 evaluator 上增加明确的 `retrieval`、`oracle_generation`、`end_to_end` 三轨和 `fixture`、`production` 执行身份。只有黄金证据诊断轨可向生成器提供 gold；生产检索和端到端输入仅包含问题及合法检索范围。
- 通过 application service 与 composition 注入真实解析、embedding、索引、reranker、回答模型和 Harness，保留预算、验证及 transcript；隔离评测索引、记忆和副作用，禁止写生产业务状态或 active skill。
- 统一规则、可替换语义 judge、人工复核的逐例评分契约；首个第三方语义 adapter 使用可选 Ragas 依赖。定义引用支持、事实正确/完整性、faithfulness、拒答、错误/缺失值及分母，禁止未评测视为通过。
- 扩展现有 A/B、matrix 和 JSON/Markdown 报告，加入配置可比性、成对差异、重复运行与不确定性、题型切片、失败链路，以及建库/请求/评分三类成本。支持只重评分，不重新调用 RAG。
- 复用现有离线与 live CI，明确固定样例回归、真实开发集评测和显式保留集验收；所有发布资格由既有确定性规则和人工流程控制，评测结果不直接发布模型、策略或 skill。
- 明确保留现有 CLI 参数与旧报告字段作为可识别的诊断/回归接口；新增版本化正式协议，不添加平行 evaluator、run/event store 或通用评测平台。

## Capabilities

### New Capabilities

- `research-rag-evaluation-dataset`: 题库制作、独立标注、原文证据、冻结版本、split、完整语料与审核契约。
- `research-rag-evaluation-execution`: 三轨执行、盲输入、生产 adapter、运行身份、隔离、预算、恢复与可比实验契约。
- `research-rag-evaluation-scoring`: 指标、可替换 judge、人工校准、分母、计费、不确定性与逐例诊断契约。

### Modified Capabilities

- `rag-evidence-eval-core-api`: 为正式实验提供结构化 options、盲输入与 adapter 注入，同时保留现有回归入口。
- `paper-rag-kernel-scorecard-report`: 为现有报告增加协议身份、显式评分状态、完整性、可比性和方向化阈值；保留原始 Paper 字段。
- `eval-ci-promotion-gates`: 明确固定样例 gate 仅证明回归合同；正式发布证据必须来自适用模式、完整题集和已审核阈值。
- `rag-live-answer-eval-workflow`: 扩展显式配置的生产评测与保留集执行，保存跳过/未完成状态并限制付费运行。

## Impact

- 复用 `F:/github/NewsRoom/backend/research/rag/evaluation/`、`F:/github/NewsRoom/framework/rag/evaluation/` 及既有 Research application/service/composition 边界。第三方评分集成是外层可选 adapter，Research 不反向导入 interfaces 或 infrastructure。
- 扩展 `F:/github/NewsRoom/data/eval/` 的审核与冻结输入，以及 `F:/github/NewsRoom/.newsroom/eval/` 的派生实验工件；沿用既有 Harness transcript/event/artifact 存储，不增加数据库或后台平台。
- 复用 `F:/github/NewsRoom/scripts/dev.py`、现有 CLI 和 `F:/github/NewsRoom/.github/workflows/`；新增运行选项和可选评测依赖不得影响普通无网络测试。
- 本变更是 `research-evidence-context-quality-foundation` 的后续评测增量；不替换其输入/Claim 资格 gate，不提前触发其上下文连续性任务，不修改 `research-local-desktop-readiness` 或 Graph 运行时所有权。

## Non-goals

- 不重写生产 RAG、不更换生产模型或默认检索策略、不实现跨论文 agent 工作流。
- 不建设独立 Web 标注平台、通用 dataset/promotion 平台或新的 run/event store。
- 不在该变更中训练 judge、使用保留集调参或宣称全量语义验证。
- 不把 20 题试点、既有 fixture 通过或缺少凭据的 skipped run 当作完整交付或产品质量达标。
