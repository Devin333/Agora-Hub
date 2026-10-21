# PDF 论文解析质量优化 PRD

## 1. 文档信息

| 字段 | 内容 |
| --- | --- |
| 产品/能力 | Research PDF Parsing Fidelity |
| OpenSpec change | `research-pdf-parsing-quality-optimization` |
| 状态 | In Progress；保真链路与评测已部分实施，正式验收仍 blocked |
| 日期 | 2026-10-21；实施更新 2026-10-22 |
| 主要使用者 | 单篇论文分析、跨来源研究报告的 Research ingestion / reader 用户 |
| 目标链路 | PDF → parser → `ResearchDocument` → chunks/evidence → analysis |
| 优先问题 | 公式恢复、混合/双栏阅读顺序、复杂表格内容 |

本 PRD 描述产品要求，不代表相关优化已全项完成或历史分数已达到目标。验收阈值须在首个解析算法改动前冻结。当前实施与实验状态见 `tasks.md` 和 `evidence/current-run-report.md`；已暴露 dev 数据的缓存转换复评不代替生产 B1、消融和独立 held-out。

## 2. 一句话概述

让 PDF 中的正文、公式和表格在进入 Research 文档模型及下游 chunks 时保持可追溯、结构完整、顺序可信；以可复现的基准和真实生产输出验证改进，而不是只提高一个脱离产品路径的 Markdown 分数。

## 3. 背景与已知基线

### 3.1 现有评测结果（B0：历史诊断，不是产品验收成绩）

在 OmniDocBench academic-literature 子集的 215 页样本上，已运行 Marker 并用官方 evaluator 评估。以下是 **page-level** 汇总；Edit distance 越低越好，CDM/TEDS 越高越好。分母是各指标实际有匹配标注的页数，不是全部 215 页。

| 维度 | 官方指标 | B0 结果 | 有效页数 | 解读边界 |
| --- | --- | ---: | ---: | --- |
| 正文 | `Edit_dist` | 0.1350 | 191 | 归一化编辑距离，不等于“准确率” |
| 展示公式 | `Edit_dist` | 0.4205 | 56 | 文字/LaTeX 字符差异诊断 |
| 展示公式 | `CDM` | 0.6369 | 56 | 字形检测匹配指标；不等于公式语义等价率 |
| 表格 | `TEDS` | 0.7338 | 97 | 表格树结构及内容的相似度指标 |
| 表格 | `TEDS_structure_only` | 0.8578 | 97 | 结构子指标；不能据此推导单元格文本正确率 |
| 表格 | `Edit_dist` | 0.2157 | 97 | 表格文本差异诊断 |
| 阅读顺序 | `Edit_dist` | 0.2746 | 212 | 顺序序列归一化编辑距离 |

公式指标在本轮官方配置中针对展示公式；inline 公式另设生产保真检查，不冒称已被该分数完整覆盖。

补充：官方汇总中的 formula CDM **sample mean** 为 0.5786（597 条评测记录），与上表的 **page mean** 0.6369（56 页）是不同统计口径；table TEDS sample mean 为 0.7522，与 page mean 0.7338 也不同。正式比较必须保持同一聚合口径，不得混用。公式/表格记录数也不等于标注对象数，评测需显式报告匹配、未匹配和失败记录。

B0 数据与脚本位置：`F:/benchmarks/AgoraHub-OmniDocBench/results/official/pred_quick_match_run_summary.json`、`F:/benchmarks/AgoraHub-OmniDocBench/results/official/pred_quick_match_metric_result.json`、`F:/benchmarks/AgoraHub-OmniDocBench/scripts/run_project_parser.py`。官方基准源码 checkout 为 `F:/benchmarks/OmniDocBench`，commit `f133a71e9e91c3621c7ce8994200a7b394a06eb3`；数据 revision 为 `aa1ee96d106dbe53d0ae59474d75c6e6d9b53fec`。

### 3.2 B0 的重要限制

- 输入为页面图片；脚本将图片逐页装入 PDF，**没有原生 PDF 文本层**。这是扫描/图片路径诊断，不能代表带文本层论文的总体效果。
- 215 个页面按 25 页一批拼成人工批次 PDF，不是 9 篇真实论文。当前不能据此声称有论文级 held-out 泛化结果，也不能按这些批次做无泄漏的训练/测试划分。
- 官方评测 Markdown 由基准运行脚本直接读取 Marker 原始 JSON blocks 生成；虽然调用了 `MarkerPdfDocumentParser.parse`，打分主输入仍不是生产 `ResearchDocument` 序列化结果，更不是实际下游 chunk 输入。manifest 记录的 normalized element 数量不能替代对 normalized 内容本身的评分。
- 脚本对缺失/越界页码使用默认值/钳制，可能掩盖页码映射问题；生产级评测导出必须校验并记录，不能“修成”有效页后算成功。
- `status=completed` 和 0 个 batch error 只表示批次执行成功，不表示每页/每个结构元素均被正确解析。
- 当时的 Marker 镜像标签为 `newsroom-marker:latest`，manifest 未记录不可变镜像 digest。B0 可作历史参照，但完整复跑还需补齐解析器、镜像、配置、数据和 evaluator 指纹。
- 215 页已被查看并用于诊断，**不能重新命名为未见过的 held-out 集**。它只可作为已暴露的开发/回归集。
- 官方总表的 notebook composite 不是本产品端到端质量分，PRD 不把它用作发布门槛或对外成绩。

### 3.3 从代码确认的风险线索（根因仍需逐例审计）

- Marker adapter 将 blocks 投影为分离的 sections、tables、equations 等；当前 `ResearchDocument` 没有明确的跨类型正文顺序主干，section 页码范围也可能无法表达跨页内容的实际起止位置。
- 表格 HTML 适配器将网格降为 `rows: dict`；空 cell 过滤、合并单元格 `rowspan/colspan`、重复/多层表头等都可能在降维时失去位置或结构。该线索需通过带原始 HTML 和图像的失败样本验证，不能预设全部错误都发生在此处。
- inline equation 的 block 类型会被归入 text；HTML-to-text、Markdown 输出以及后续 chunking 的处理需要端到端检查，确认公式标记、位置和文本邻接没有损失。
- adapter 目前主要按上游 block 列表遍历，几何 bbox 已可转换为 PDF 坐标，但“上游顺序是否可靠”及“何种页面适合几何重排”必须以错误样本为准。

## 4. 用户问题与目标

### 4.1 用户问题

1. 读者在单篇论文中可能看到公式缺项、上下标/矩阵结构错误，或公式被正文清洗/切块吞掉。
2. 多栏、跨栏标题/图表和跨页内容可能被拼接成错误阅读顺序，进而污染章节、引用位置及 RAG chunks。
3. 复杂表格的空单元格、合并/多层表头、行列关系、数字与符号需要保留；仅保存扁平文本或视觉描述不足以回答细粒度问题。
4. 团队需要知道错误来自原始 OCR/backend、项目归一化、chunk 序列化还是评测脚本，并在相同输入上比较版本。

### 4.2 产品目标

- 在统一、版本化的文档结构中保留页、block、文本/公式/表格、阅读顺序、几何位置、父子关系和来源定位。
- 让正式分数能分别回答“backend 原始输出如何”“生产 ResearchDocument 如何”“下游消费是否丢失结构/顺序”。
- 先按错误类别和样本诊断，再有针对性改善双栏阅读顺序、公式保真、复杂表格网格；低置信时保留原始信息并暴露不确定，而非猜测。
- 为 single-paper 和 cross-source 两种流程提供相同的 PDF parsing provenance、locator 和可比质量指标；下游 RAG 质量继续复用既有 RAG evaluation，不另建第二套 RAG evaluator。
- 对 schema、执行配置、数据、失败分母、运行时间与资源成本提供可回放证据。

## 5. 范围

### 5.1 本变更包含

1. **可追溯的结构化解析 IR**：页序、block 顺序、block kind、原始/规范化文本、bbox 坐标类型、来源页、父子引用、阅读顺序置信/状态；以 Research domain/parser adapter 为 owner，不建立和业务模型脱节的影子 parser。
2. **版面与阅读顺序**：针对单栏、双栏、混合栏、跨栏元素和跨页续接建立可验证的顺序处理；仅在可解释的高置信条件下重排，保留 backend 顺序与决策证据。
3. **公式保真**：区分 inline/display、保存 LaTeX 源、位置/页码/上下文关系；验证下游 normalization/chunk/Markdown 路径不静默删改公式。
4. **表格保真**：保存规范化网格及 cell 坐标、行列索引、表头属性、row/col spans、原始 cell 内容和 source locator。现有 `rows: dict` 可以作为明确标注为 lossy 的派生投影，不作为唯一真值。
5. **确定性质量 gate 与受控恢复**：输出缺页/异常页码/无 bbox/公式无效/表格结构不完整/内容丢失等机器可检原因；任何重试或 ROI 重解析均复用既有 Harness/execution budgets，有限、可追溯、无成功伪造。
6. **分轨评测和运行清单**：固定原始 backend 评测、生产 normalized document 评测，以及到真实 chunk boundary 的一致性检查；报告分母、失败类别、切片与版本指纹。
7. **逐例错误分析工具/报告**：可将官方指标异常映射回 source page、预测元素、gold match 和 raw artifacts，便于人工快速复核。

### 5.2 不包含

- 更换或重新引入 MinerU；保留当前后端决策，Marker、PyMuPDF deterministic fallback 与 LaTeX-first arXiv compiler 的所有权不变。
- 新建 OCR/agent/evaluator 平台、数据库、持久化层、通用评测框架或新的 RAG evaluator；复用现有 Research、Harness、parser bake-off 和 RAG evaluation 能力。
- 用 LLM 决定 workflow route、质量是否通过、是否入库/发布或是否升级 active skill。解析修复结果不得直接修改 active skill。
- 将图片描述当作表格单元格真值；将视觉/LLM judge 的公式语义判断当作官方 CDM/TEDS 替代指标。
- 在标注没有证据时凭空补公式、文本或表格单元格；将无法解析自动转为成功。
- 对已经检查过的 215 页宣称 unbiased held-out。
- 此次规划本身不修改解析实现、不重跑完整 OmniDocBench、不宣称得到优化后结果。

## 6. 功能需求

### FR-01 解析产物可追溯

每个 PDF page/block 结果要可追溯到 source hash、parser/backend/version/config、输入页和原始 artifact；区分物理 PDF 页码、印刷页码和 dataset page id。无效或未知值必须显式保留为 unknown/error，不能通过默认页码或边界钳制隐藏。

### FR-02 顺序保真

统一表示保存 backend order 和产品采用的 reading order。排序逻辑必须处理栏、跨栏块、标题、图表、脚注及跨页边界，并记录置信/原因；低置信时不声称已恢复正确顺序，而是保留原始顺序、标记 degraded 或触发有界人工/再解析路径。

### FR-03 公式保真

display 和 inline 公式都应保留原始 LaTeX/公式内容、源位置、页码和上下文锚点；文本抽取、HTML/Markdown 序列化及切块前后可做 round-trip/覆盖校验。不得用“能成功渲染”替代“内容正确”。

### FR-04 表格保真

表格规范化结果要能表示 ragged/missing cells、明确为空的 cell、合并单元格、表头行/列和 cell 内公式；数值、正负号、小数点、单位、上下标/脚注不能被 trim、去重或重新映射成其他列。原始 HTML/JSON 与 grid 必须可审计。

### FR-05 生产路径对齐

评估器必须能够以生产 parser 的正常 adapter/execution identity 运行，并直接评估它产出的 `ResearchDocument` 及实际使用的下游 chunk serialization。离线 raw-backend 诊断可以保留，但不得冒充产品流程成绩。

### FR-06 评测可复现、失败不消失

每个样本都要有 planned/running/succeeded/failed/skipped/invalid 等终态之一；失败、空输出、无法匹配、超时和异常要计入报告分母/状态。报告保存 dataset/gold/evaluator/parser/image/config/schema/chunker/normalizer/hardware 指纹及每页指标；分母按 page/object/sample 明确区分。

### FR-07 回归与验收

不可测的 B1 维度不能借 raw 输出虚构成绩；须在优化前冻结可测的绝对门槛/保真要求或标记该维验收 blocked，不把首次可测说成相对提高。

以已暴露 215 页作为开发/回归材料；建立来源文档级、去重且未用于调参的 held-out 集。held-out 使用前冻结版本及验收阈值。对官方指标分别报告 page-level 与 object/sample-level，必要时做按文档成对 bootstrap 区间；不以一个 composite 分数掩盖关键回归。

### FR-08 下游收益和成本可见

验证结构化元素从 `ResearchDocument` 进入实际 chunks 后没有丢失必要内容、顺序和 locator；端到端检索质量使用现有 RAG evaluation 交叉引用。报告解析延迟分布、失败率、GPU/CPU 或显存信息（运行环境可得时）、额外恢复尝试及资源开销。

## 7. 验收标准（建议值；必须在 M0 冻结）

以下是**候选目标**，不是声称已实现的分数。由于生产规范化轨 B1 尚未测量，第一阶段必须先跑 B1，再在未查看优化结果前确定并签入 acceptance profile；如需调整，必须发生在首个解析算法改动之前，并记录理由。

| 指标 | 候选变化目标，相对冻结 B1 | 约束 |
| --- | --- | --- |
| formula CDM | 提升至少 +0.05 | 同官方版本、相同 page/object 分母口径 |
| formula `Edit_dist` | 至少降低 10% | 公式无效/丢失率不能升高 |
| table TEDS | 提升至少 +0.03 | 与表格内容 `Edit_dist` 联合读，不能只改善结构 |
| table `Edit_dist` | 至少降低 10% | 对 hard tables 和语言/布局切片检查回归 |
| reading-order `Edit_dist` | 至少降低 15% | 双栏/混合栏改善且单栏无显著退化 |
| body text `Edit_dist` | 不得恶化超过 +0.01 | 预先固定请求/可评标注集合；显式报告匹配分母及缺失输出，不只筛双方成功样本 |
| 样本完整性 | 100% requested pages 有终态；关键转换 fixture 无静默丢失 | 失败不得作为通过或从分母中删除 |
| 性能 | 候选约束：同机中位耗时 ≤ B1 的 1.5 倍，p95 ≤ 2 倍 | 首次读取/缓存、并发、GPU 必须相同并分别报告 |

M0 若发现指标方差、样本量或匹配缺失不足以支撑这些阈值，须建立可测的预注册替代阈值和置信规则后再开始优化；不得在看过 held-out 结果后修改目标。最终验收还要求：数据无页面/论文级泄漏；重要页/对象具备 locator；失败和降级可解释；真实生产链路验证通过；没有对 unrelated parser/RAG/report 的回归。

## 8. 用户流程与产物

1. 提交一个真实 PDF 到既有 ingestion/parser composition。
2. parser 执行产生带 attempt/receipt 的原始 backend artifact。
3. domain adapter 生成有版本的有序文档结构及 Research entities；deterministic quality gate 验证页数、来源、类型、顺序、公式/table 内容和 locator。
4. gate 若通过，正常进入既有 chunk/evidence 路径；若有确定性、可恢复的问题，在预算内重试指定阶段；仍失败则 degraded/quarantined 并报告原因，不交给模型“补成成功”。
5. benchmark runner 按同一 frozen input 同时生成 raw backend 诊断和生产文档轨结果；需要时输出逐页/逐对象错误证据。
6. 对预先冻结的 unseen document set 做发布验收，使用既有 Research quality / RAG evaluator 评估 downstream，不自动修改生产策略或 skill。

## 9. 里程碑

- **M0 — 证据与基线**：修正/标注当前 benchmark limitations；固定 OmniDocBench/evaluator/Marker 的不可变版本；给 215 pages 建唯一审计 manifest；验证 source/document grouping 和重复项；以历史 raw artifacts 复查 B0，并建立新协议下可比的 raw baseline 与真实生产 `ResearchDocument` B1；预注册目标和测试集规则；收集并人工分类代表性坏例。
- **M1 — 结构真值与来源定位**：定义 versioned content-block / table-grid contract；保留 backend order、page/bbox/source lineage；建立 serializer/chunker 保真测试；完成存量 PDF artifacts 的版本识别及有界重解析/失效策略。
- **M2 — 阅读顺序**：以人工校验的单栏/双栏/混合栏/跨栏/跨页样例实现和比较候选顺序策略；高置信才采用，计算切片分数及退化项。
- **M3 — 公式与表格**：先定位 Markdown/normalizer/HTML/grid 导致的损失，再做局部公式/表格恢复；基于真实错误样本和结构 fixture 联合评估；限制任何 ROI retry 和资源增长。
- **M4 — 验收与受控启用**：同版本/同硬件 paired run；对独立 doc-heldout 做最终 gate；核对 single-paper 与 cross-source ingestion；报告指标、失败、成本、逐例和 rollback plan；未过 gate 保持显式 profile opt-in，不改默认。

## 10. 依赖与风险

- OmniDocBench 当前 215 个页面不构成 document-level held-out，且缺少 native text PDF；需补充独立论文 PDF 与适用于 parser 的标注/分组证据。
- 现有 gold 元素与产品抽取结构可能定义不同，需先审计 matching/漏标；评估失败不能被误归因为 parser。
- 同一页存在多个指标单位，可能样本量较低；以区间和逐例统计避免过拟合小切片。
- 新 IR 增大持久化和重算成本；以 artifact 版本化、摘要/引用与显式 reparse policy 控制，不再造第二套 document store。
- 几何重排会改善双栏却可能破坏跨栏/脚注；保留原始序列、受控 opt-in、所有布局 slice gate 和 deterministic rollback。
- LaTeX / HTML 是潜在不可信输入；使用既有安全执行边界，禁止任意 shell/network、未过滤脚本、任意路径读写和无上限 retry。
- 与正在进行的 parser backend 移除、parser cascade bake-off、RAG evaluation changes 协调接口，但不重引 MinerU、不重复 evaluator/store、不覆盖并行工作的文件。

## 11. 发布决策

只有在 M0 的生产基线和阈值签署、M1 数据契约与失败分母验证、M2/M3 关键 slice 达标、M4 独立 document holdout 通过且 production execution receipts 完整时，才可申请将新 profile 提升到默认运行配置。发布由确定性 gate 和人工 owner 决定；LLM 只能提供候选解析内容或诊断，不能批准 gate、路由或 promotion。rollback 按不可变 parser/schema/profile fingerprint 恢复上个已验证版本；旧 parser artifacts 不得与新 schema 结果静默混合。
