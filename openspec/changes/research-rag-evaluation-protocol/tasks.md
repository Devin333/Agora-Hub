本清单用于后续实现；本次仅提交规格，所有实现、人工标注及 live 验收任务保持未完成。按依赖顺序推进；代码就绪不等于真实数据或质量验收就绪。人工审核、材料使用范围、凭据、预算和正式阈值必须取得真实输入，不得用 fixture 或自动审批替代。

每个代码提交前运行范围匹配的检查及 `python -m scripts.dev smoke`；检查失败须修复根因，不能削弱断言或以本变更外的脏文件替代交付。实现过程中保持 `openspec validate research-rag-evaluation-protocol --strict` 通过。真实实验的质量结果允许为 `fail`；不得为了让结果变成 `pass` 而泄漏 gold、修改 held-out、隐藏失败或反复挑选输出。

| Capability | 主要任务组 |
| --- | --- |
| `research-rag-evaluation-dataset` | 1–3、12 |
| `research-rag-evaluation-execution` | 1、4–5、13 |
| `research-rag-evaluation-scoring` | 1、6–9、13 |
| `rag-evidence-eval-core-api` | 1、4、10 |
| `paper-rag-kernel-scorecard-report` | 6、8–9 |
| `eval-ci-promotion-gates` | 9、11、13 |
| `rag-live-answer-eval-workflow` | 5、10–11、13 |

## 1. 契约与现有入口基线

- [ ] 1.1 核对既有 evaluator、application/composition、报告、CLI 和 CI 的实际调用链，记录复用点及与 `research-evidence-context-quality-foundation` 的边界；不另建 evaluator 或 run/event store。
- [ ] 1.2 固定 `DatasetManifest`、`EvaluationCase`、`EvaluationRun`、`EvaluationScore` 的版本化 schema、枚举、显式空值、内容摘要和跨记录引用规则，统一既有 `EvidenceQAPair` 的消费边界。
- [ ] 1.3 实现 `QuestionInput` / `ReferenceLabel` 的物理及类型分离、严格字段校验和清洁运行控制参数；加入完整 case、额外 gold 字段及答案册路径/回调不能进入 A/C 的负向测试。
- [ ] 1.4 扩展既有结构化 evaluation options 与 application service 接口，明确 track/profile、manifest、注入端口、工件引用和恢复选项，保证库调用不依赖 CLI argv。
- [ ] 1.5 建立指标注册合同，声明 metric/version、输入、适用性、scope、粒度、方向、聚合与缺失策略；实现 nullable score 到有效 `MetricValue` 的受控投影。
- [ ] 1.6 为旧命令参数、诊断返回码、旧阈值算法和基础报告字段建立兼容测试；验证旧 live/real-corpus 调用不会自动取得 production 资格。

## 2. 独立题库制作、来源和冻结

- [ ] 2.1 实现独立候选制作与导入流程，记录 `abstract_only` / `real_user` / `fulltext_targeted` / 合成来源；保存出题输入指纹、问题冻结时间和独立全文标注记录，禁止正式 run 动态造题。
- [ ] 2.2 实现 `draft -> annotated -> reviewed -> frozen` 生命周期及人审终审记录；将材料缺失、无法裁定与经完整原文核查的 `unanswerable` 分开，拒绝 LLM 自动冒充审核者。
- [ ] 2.3 实现版本化原文锚点、原子 `reference_claims` 及必要证据组/替代充分集合校验，覆盖 PDF/LaTeX、段落、表格、公式与图形定位，不只绑定 chunk ID。
- [ ] 2.4 实现完整源语料 manifest 与独立 scope 校验，保留非 gold 内容和声明的干扰论文；区分源材料不全与待测 parser 漏证据，后者保留失败及分母。
- [ ] 2.5 实现按 parser/chunker/index snapshot 生成的锚点映射工件，支持跨 chunk、部分覆盖、等价与歧义；增加过期映射、重复证据、有效原文被解析丢失的测试。
- [ ] 2.6 实现 paper/version/near-duplicate family 分组、dev/heldout 成员校验及暴露史；允许合法索引 heldout 原文，但拒绝标签、答案、题目派生笔记或调参反馈污染。
- [ ] 2.7 实现冻结摘要、只读发布和新版本纠错校验，保持旧数据/评分可追溯；覆盖原地篡改、成员错配、未知来源和将旧题重切为盲测的拒绝场景。

## 3. 既有 79 题审计与开发试点材料

- [ ] 3.1 对 `F:/github/NewsRoom/data/eval/golden_set.json` 和现有语料做只读盘点，输出题目/论文/暴露史清单，并将 79 题固定划分为四个不重叠审计批次，每批不超过 20 题。
- [ ] 3.2 完成第一批来源、可答性、参考事实和证据位置核查，逐题记录 `reviewed` / `retired` / `needs_review` 去向及真实审核信息，不回填不存在的历史。
- [ ] 3.3 按同一审核协议完成第二批审计，保存原始 case ID 与新契约的映射及未解决项。
- [ ] 3.4 按同一审核协议完成第三批审计，保存原始 case ID 与新契约的映射及未解决项。
- [ ] 3.5 按同一审核协议完成第四批审计，核验四批并集恰为原 79 题、无遗漏和重复，输出逐题迁移清单。
- [ ] 3.6 从已复核开发候选中选定约 20 题试点，补齐标准事实、充分证据与人工终审，覆盖可答/拒答及目标题型；缺口只由合格的新开发题补充，不挪用 heldout。
- [ ] 3.7 冻结试点 dev 数据和完整语料，验证 questions/references 分离、schema、摘要、split 与映射；明确该批是开发材料而非最终盲测或完整交付。

## 4. 三轨与真实产品路径

- [ ] 4.1 在 Research 定义最小 evaluation application ports，由外层 composition 注入真实服务和可选评分 adapter；加入禁止反向导入 interfaces/infrastructure/legacy Paper Radar 的架构测试。
- [ ] 4.2 实现 production preflight 与实际 component provenance 校验，记录源文件、解析、embedding、索引、reranker、generator 身份；生产请求发现 hash adapter、固定回答或 gold-only corpus 时在付费前拒绝。
- [ ] 4.3 接入 `retrieval` 轨，调用真实检索/重排路径并保存原始候选、重排结果和声明的上下文投影；通过测试确认不调用回答模型。
- [ ] 4.4 接入 `oracle_generation` 轨，仅对已审核可答题提供充分原文证据而不提供答案/事实标签，沿用实际 answer worker/prompt/budget；验证证据过长、缺少模态能力和 B 结果按生成配置复用。
- [ ] 4.5 接入 `end_to_end` 轨，通过产品 `rag_ask(generate=True, gated=True)` 及既有 Harness 执行，保留 gate 拒绝和重试；验证不能用 direct generator 或 B 的上下文替代。
- [ ] 4.6 在既有 transcript/artifact 设施中保存逐阶段 trace，包含实际文本/图像输入、裁剪、答案、引用、usage 和失败；验证能区分未召回、召回后丢弃、模型已见但答错。
- [ ] 4.7 实现评测 tenant/index/memory namespace 和 cache 初态隔离，验证只写评测工件，不写生产记忆、不发布业务结果、不修改 active skill。

## 5. 实验身份、有界执行与恢复

- [ ] 5.1 实现 experiment manifest 的计划槽位和单元主键，区分 variant/repetition/attempt；固定 Git revision、相关工作区内容、配置及实际 adapter 指纹，正式发布证据拒绝不可重建版本。
- [ ] 5.2 将总请求/token/费用、逐次 timeout、运行 deadline、并发、Harness replan/retry 接入共同预算，停止后保留未执行项与原因，恢复不重置消耗。
- [ ] 5.3 在既有持久化设施中实现外部调用前的 attempt intent 和响应后的结果/usage 提交，验证已完成单元恢复时不再请求、不覆盖历史输出。
- [ ] 5.4 实现供应商已接收但响应未落盘的 uncertain attempt 处理，默认不自动重复；测试对账或显式允许重试策略及剩余预算，不承诺不可保证的 exactly-once。
- [ ] 5.5 实现 index/request/judge 三类账目，记录实际 usage、带版本价格表的估算、未知费用、失败与重复调用；验证缓存复用不重复收费，未知不变成零。
- [ ] 5.6 加入取消、预算中止、部分工件、恢复和模态不足的集成测试，确认完整计划、原生 phase transitions 和失败 trace 可审计，且缺失阶段不能产生正式 pass。

## 6. 确定性评分与完整分母

- [ ] 6.1 复用通用指标并实现 `hit_at_k`、`mrr`、标准 `ndcg_at_k` 的冻结相关性/计分单位/去重口径，固定 `k=[1,3,5,10]` 等配置；测试无命中、重复排名和固定 ideal 分母，不混入另行定义的组排名指标。
- [ ] 6.2 实现按必要组及替代充分集合计分的 `evidence_recall_at_k` 和 `context_evidence_coverage`，加入跨 chunk、替代证据、缺少另一必要组、上下文截断和 parser 漏证据的手算测试。
- [ ] 6.3 实现引用解析与 claim/原文精确绑定、数值/单位/显式实验条件规则，保存 rule/version 和证据；无法确定的语义转 judge/复核，不以格式正确或关键词相同判支持。
- [ ] 6.4 实现事实、完整性、faithfulness、citation support/coverage 的独立观察及输入边界，验证事实正确但无上下文支持、忠实但遗漏、引用不支持和无引用事实答案等场景。
- [ ] 6.5 实现 reviewed answerability 驱动的正确拒答率与误拒答率，版本化部分回答/夹带结论识别；测试空答案、runtime error、零分母和应引用未引用不能自动满分。
- [ ] 6.6 实现 `ok/error/not_applicable/needs_review`、planned/applicable/valid/error/NA/pending 计数及端到端 known_success/known_failure/unknown 分母；验证 runtime 失败不删题，judge 失败不冒充模型错误或通过。
- [ ] 6.7 实现分阶段耗时、P50/P95、样本量、冷热缓存、并发和 timeout/SLO 记录；报告成功请求分位数的条件范围，不以幸存请求掩盖失败。

## 7. 可选语义 judge 与人工校准

- [ ] 7.1 实现 Research-owned judge port 和外层可选 Ragas adapter，锁定实际测试过的依赖版本并保留替换边界；普通无网络测试不要求第三方包或凭据。
- [ ] 7.2 为事实、faithfulness 和引用分别构造最小评分输入，保存实际 provider/model、prompt/rubric、metric/adapter 版本、原始判断及证据；完成 schema/跨字段/模态能力验证。
- [ ] 7.3 将 judge 调用接入有界执行与独立成本账目，测试不可用、非法输出、超时、重试耗尽及 text_subset；不得静默降级为规则分或将子集结果写成全题通过。
- [ ] 7.4 从非 heldout 材料制定约 30 条人工校准计划，区分调校与冻结验证、随机与冲突抽样；先收集盲化的独立人工标签，再比较 judge，保留 single-review 或真实多人分歧证据。
- [ ] 7.5 在上述开发材料上完成一次有据可查的 judge/rubric 校准与独立验证，保存按抽样方式分组的一致性和局限，冻结经批准的评分 profile；未完成真实人工审核时保持本任务未完成。

## 8. 复核工件与只重评分

- [ ] 8.1 实现带 case/run/score revision 和盲化方案标签的审核导出，首轮人审隐藏 judge 结论；保留原文、实际上下文、回答及目标，区分评分复核与独立 gold 纠错。
- [ ] 8.2 实现审核导入及 revision 校验，拒绝未知/过期 ID、无审核者或无效值；只追加评分修订，不覆盖回答、不由导入触发发布。
- [ ] 8.3 实现 score-only，从完整且哈希匹配的不可变 run 工件重新评分；只允许兼容标签修订，输入改变或缺失时明确失败，不补跑 parser、检索或回答。
- [ ] 8.4 实现内容寻址评分缓存，将 dataset/gold、实际上下文、答案/引用、来源/映射、judge/rubric/metric、scope 和人工修订纳入身份；测试相同答案不同上下文不复用错误分数。
- [ ] 8.5 加入审核往返、gold 新版本纠错、评分版本切换和只重评分集成测试，证明原始输出/旧评分不变、比较方案同版本，只有新 judge 调用增加费用。

## 9. 多方案比较、报告与确定性资格

- [ ] 9.1 提供 `current_baseline`、`dense_only`、`hybrid`、`hybrid_reranker` 的显式配置，核验名称对应实际通道/权重/重排实现；缺少可运行实现时保留 not_run，不用重命名伪造方案。
- [ ] 9.2 实现 comparison fingerprint 和 `allowed_differences` 校验，固定相同题集、语料、生成器/prompt、最终 top-k/token budget、初态及计时条件；chunking/embedding 比较仅允许预声明变量及派生索引变化。
- [ ] 9.3 扩展既有 A/B 和 matrix，同题配对、题内聚合重复、按论文 cluster bootstrap 或预声明等价方法给出区间；加入小样本、缺配对、重复输出和 oracle 不参与排名的测试。
- [ ] 9.4 扩展现有 JSON/Markdown 报告，保留旧字段并增加总体/题型/模态切片、计划计数、逐题差异、失败链路、实际 provenance、账目与可比性，不只输出单一总分。
- [ ] 9.5 实现 `min/max/delta_min/delta_max/min_coverage`、最低独立样本与版本化审核 policy 的确定性门禁，分离执行 status 和 `pass/fail/inconclusive/not_applicable`；未配置阈值或必需判分未知不能通过。
- [ ] 9.6 为不可比配置、judge 缺失、完整 runtime 失败、成本上限、退化约束和空阈值建立报告/门禁测试，证明指标有效不等于通过、通过也不自动 promotion/publication。

## 10. 统一 CLI 与使用说明

- [ ] 10.1 在现有 `python -m scripts.dev rag-eval` 命令组实现 `validate-dataset`，通过 application service 输出 schema、审核、材料、split、摘要和映射诊断，不进行付费调用。
- [ ] 10.2 实现 `run` 的 manifest、track/profile、预算与恢复选项，按既有 composition 注入实际路径；验证 preflight、部分执行和正式失败/未完成的退出码。
- [ ] 10.3 实现 `score` 与 `report`，接受已落盘运行及显式评分版本/预算，验证不会隐式重跑 RAG，输出保留历史及配置身份。
- [ ] 10.4 实现 `export-review` 与 `import-review` 的 application service 入口和离线往返测试，避免 interface 直接访问底层 store/executor。
- [ ] 10.5 编写基于已实现命令的制作、三轨、对照、复核、恢复和报告使用说明，区分 fixture/dev/heldout；运行 CLI 帮助、端到端离线示例及旧入口兼容测试，不把示例成绩写成真实测量。

## 11. 分层 CI 与运行控制

- [ ] 11.1 扩展既有 push/PR 无网络 fixture gate，覆盖数据隔离、映射、三轨合同、计数、报告、架构和 CLI；fixture provenance 与产品成绩分开，不引入 live 凭据读取。
- [ ] 11.2 为既有定期/手动 live workflow 增加显式生产 dev manifest、审批后的数据发送范围及成本预检，缺预算/材料/真实 adapter 时在调用前停止。
- [ ] 11.3 加入显式 heldout qualification 入口，要求冻结配置、用途、审核批准引用和暴露记录；常规定期任务不得自动使用 heldout 调参或验收。
- [ ] 11.4 实现失败、skip、中止时仍保留/上传可用工件；测试旧诊断允许 skip 而正式必需阶段缺失返回非零/inconclusive，不以 fixture pass 补足。
- [ ] 11.5 支持 workflow 的 score-only 及独立 judge 预算，验证不重复生产请求；提供停止新任务和回退入口的操作说明，保留历史工件及旧诊断路径。

## 12. 新增独立保留集交付

- [ ] 12.1 确认未用于本项目调参的新论文族、原始材料/附件完整性、使用范围和人工安排，冻结 heldout 候选来源及五个制作批次；暴露史未知的材料不冒充未使用。
- [ ] 12.2 按独立提问/全文标注/人审流程完成第一批 10–20 道有效保留题，记录实际出题方式、原子事实、充分证据及合理无答案题；不为数量而降低质量。
- [ ] 12.3 按相同协议完成第二批 10–20 道有效保留题，检查与 dev 及其他批次的 paper family 隔离。
- [ ] 12.4 按相同协议完成第三批 10–20 道有效保留题，复核已声明的方法、实验、表格、公式、跨段落及无答案题型覆盖。
- [ ] 12.5 按相同协议完成第四批 10–20 道有效保留题，保存真实审核人与不可裁定候选的排除理由。
- [ ] 12.6 按相同协议完成第五批 10–20 道有效保留题，合并得到 50–100 道新的人审题；若需调整目标，先显式修订范围，不能把试点、旧题或 needs_review 凑入数量。
- [ ] 12.7 冻结保留题库、完整源语料及 annotation/split 版本，运行全部数据校验并独立核对证据；题目/标签不进入生产索引、模型提示、缓存或普通开发反馈。

## 13. 真实运行与最终验收证据

- [ ] 13.1 在约 20 题开发试点上执行一次有预算且获准的三轨 production 纵向验证，复核实际 adapter、Harness trace、输入隔离、运行费用和人工可定位性；失败先修复，不称为最终 heldout 成绩。
- [ ] 13.2 在冻结 dev 上完成四种适用方案及声明的重复计划，B 按生成配置共享；保存全量失败、逐题差异和置信范围，不以 fixture 或近似 adapter 替代实际测量。
- [ ] 13.3 根据 dev 基线和校准证据批准质量/退化/费用阈值、最低独立样本、覆盖要求与统计方法，冻结生成/评分/比较配置及代码版本，再批准 heldout 运行。
- [ ] 13.4 按批准 manifest 执行独立 heldout 三轨与适用方案，保留题目暴露记录、完整计划分母、原始结果与计费；质量失败照实报告，不在同一保留集上调参后继续称盲测。
- [ ] 13.5 按冻结审核计划复核成功、失败及 judge 分歧，完成必需裁决/统一重评分，交付总体、切片、成对差异、失败归因、费用与时延报告；未知项未解决时保持正式验收 inconclusive。
- [ ] 13.6 核对既有 79 题去向、新增 50–100 题、语料版本、校准、三轨/四方案状态及 CI 工件，形成交付清单；人工、live 或必需方案尚未完成的任务不得勾选或用 20 题试点宣告全量交付。
- [ ] 13.7 执行范围匹配的单元/合同/架构/CLI/离线集成检查及 `python -m scripts.dev smoke`，运行严格 OpenSpec 校验，验证回退不删除历史证据；提交聚焦变更，只有任务与证据真实完成后才申请归档。
