## 1. M0：盘点历史证据与数据边界

本清单包含实施与验收任务；勾选仅代表对应范围已有可核验证据，不表示整个优化或发布验收通过。当前实施已经开始；交接文档的“仅规划”描述保留为历史快照。FR 对应 [PRD](F:/github/Agora-Hub/openspec/changes/research-pdf-parsing-quality-optimization/prd.md)；D 对应 [design](F:/github/Agora-Hub/openspec/changes/research-pdf-parsing-quality-optimization/design.md)。实施前先核对并行 parser removal / RAG evaluation 的最新状态，不覆盖其他工作的写集。

- [x] 1.1 审计并固定当前 parser composition、Marker image/weights/schema、原始 benchmark runner 与实际 execution identity；保存 repo commit + dirty 内容指纹，历史无法恢复字段明确 unknown（FR-01/06，D1/D7）。
- [x] 1.2 为现有 215 页建立逐页输入 hash、原始 source/document/version-family grouping、exposure 和 batch mapping manifest；不能确定源文档的页标 unknown，不把人工批次当真实论文（FR-06/07）。
- [ ] 1.3 根据本地 raw JSON、ResearchDocument 路径和官方逐例结果抽检公式/顺序/表格各至少 10 个有效坏例及至少 10 个正常对照；绑定原页、预测、阶段、gold-match 和根因，数量不足如实记录（FR-02/03/04，D8）。
- [ ] 1.4 设计独立真实多页/native/raster PDF 文档保留集，核对许可、文档级近重复与标注规则；冻结前登记 dev/held-out 边界，已暴露 215 页只能是 regression（FR-07，D7）。
- [x] 1.5 审阅现有 artifact/document/chunk persistence 契约和受影响消费者，确定新结构的大小上限、版本失效和 scoped rollout 边界，避免引入第二套 store/evaluator（FR-01/05/08，D2/D9）。

实施顺序偏离记录见 `evidence/protocol-deviation-and-current-evaluation.json`：当前 `B1_current` 不得冒充优化前生产基线，事后冻结不得冒充原始预注册；缺少独立标注和原始预注册证据时正式验收保持 blocked。

## 2. M0：对齐两轨评分并冻结生产基线

本组完成之前不开展解析算法调优；必要 evaluator/只读 exporter 的开发应固定协议，不在导出器中做识别修复。

- [x] 2.1 在现有 evaluation 入口增加带版本的 parsing manifest/outcome 契约与校验测试，记录 track、执行身份、输入模式、全部版本指纹、预算和 page/object/sample 分母（FR-05/06，D1/D7）。
- [x] 2.2 接入冻结官方 OmniDocBench scorer 的隔离可选 adapter；保存官方原始结果和 runtime summary，普通无网络测试不依赖下载模型或数据（FR-06）。
- [x] 2.3 实现只读 `backend_raw` 导出及显式 page/schema 映射校验，移除正式导出中的猜页/clamp；保留旧 raw artifacts 的诊断身份，不覆盖历史输出（FR-01/06，D1）。
- [x] 2.4 为现有实际 ResearchDocument/合法规范 artifacts 实现 `production_document` 导出；添加“raw 有内容但 document 缺失”“旧结构缺顺序不能评分”的测试，禁止从 raw/gold 偷补（FR-05，D1）。
- [ ] 2.5 通过既有 application/composition/execution adapter 跑冻结当前 B1，并捕获实际 chunker/store 的 `downstream_projection` baseline；两轨按同输入报告错误/不可评，不能把 diagnostic subprocess 冒充 production（FR-05/08）。
- [ ] 2.6 在首个解析算法改动前签入 acceptance profile：指标与方向、可测性/天花板、绝对替代阈值、分母/可比性、置信/最小样本、held-out 使用、时延/资源和恢复预算；profile 不完整则阻塞正式验收（FR-07/08，D7/D8）。

## 3. M1：统一结构和来源保真

- [x] 3.1 在 Research domain 定义 versioned page/block/inline-math/table-grid 契约及严格校验；复用现有实体 identity、lineage 和 artifact refs，不增加 benchmark-only 业务模型（FR-01/03/04，D2）。
- [x] 3.2 调整 Marker adapter 保留 backend parent/child identity、source order、caption 关系和节点所有权；覆盖嵌套父子重复、同字不同位置和不支持类型的完整性测试（FR-01/02，D2）。
- [x] 3.3 集中实现 backend schema 的一基页码/坐标转换，复用现有 PDF rect helper；测试 page_idx=0、越界/缺失页、旋转/缩放、未知坐标不猜测（FR-01，D2）。
- [x] 3.4 修复 section 跨页覆盖及局部 block locator 投影，记录实际页范围；加入连续多页与人工 batch 跨文档不得拼接的回归（FR-01/02，D3）。
- [x] 3.5 通过既有 persistence 保存/加载新结构并验证 checksum、ref 和版本 round-trip；旧 artifact 能力标 unknown/versioned，不无声补成新已验证产物（FR-01/06，D2/D9）。

## 4. M1：确定性质量控制与安全恢复边界

- [x] 4.1 扩展现有 DocumentQualityProbe 的纯规则/报告，分开 execution、结构/来源和内容完整性；按版本化 policy 映射 parsed/degraded/quarantined，测试进程成功但内容失败（FR-01/06，D6）。
- [ ] 4.2 在现有 Harness/execution path 接入有限 parser/ROI recovery 请求与 typed settings allowlist，冻结 max_turns/replans/attempt/page/region/time 约束；默认不启用未授权识别 provider（FR-05/08，D4/D6）。
- [ ] 4.3 接入既有 durable attempts/phase events/receipts，覆盖预算耗尽、取消、超时终止未知、重启 replay 不再调用 live backend 的测试；禁止直接 subprocess 旁路（FR-06/08）。
- [ ] 4.4 为公式渲染、HTML 处理和 crop 建立受限输入/路径/网络/资源规则与恶意 fixture；测试 shell escape、script、事件属性和文档注入不会越权（FR-03/04，D6）。
- [ ] 4.5 实现原版/候选及采用原因保留与确定性检查，证明可渲染或内容更长不自动等于正确；未知/不可靠候选不覆盖为已验证事实（FR-03/04，D4/D5）。

## 5. M2：有证据约束的阅读顺序

- [x] 5.1 基于 M0 坏例实现明确适用域的 layout/band 候选检测，保留 backend order；低置信、missing bbox、旋转或冲突布局输出可诊断不确定（FR-02，D3）。
- [x] 5.2 实现栏内顺序、纵向 band 的栏序及跨栏块边界合成，维护 caption/inline/parent 约束和稳定 tie-breaker；不全页盲目 `(y,x)` 重排（FR-02）。
- [x] 5.3 添加单栏、双栏、中部跨栏表格、标题/脚注/公式、同 y block 与跨页续接的 fixture 及真实 dev 回归，验证 block coverage、节点唯一性和 locators（FR-01/02）。
- [ ] 5.4 在不改变 parser/evaluator 指纹的独立候选中跑单项顺序 ablation，保存各布局/语言 slice 分母、逐例修复与退化，不将 text ED 差异直接当顺序根因（FR-07，D8）。

## 6. M3：inline/display 公式保真与恢复

- [x] 6.1 保留数学内容的源字段/类型、LaTeX 分隔、编号、正文 offset 与邻接，修复被证实的 HTML-to-text/normalizer 损失，不做无依据的数学语义改写（FR-03，D4）。
- [x] 6.2 添加上下标、分式、多行 align/matrix、比较符、escaped delimiter、cell math 及原始/规范化 round-trip fixture，并关联真实失败公式回归（FR-03）。
- [ ] 6.3 按 M0 错误证据评估已有获准 backend 的有限公式 ROI candidate；需要 provider/模型变更时先明确依赖和预算，未经授权保持关闭；无可靠收益则记录未达目标而非伪造恢复（FR-03/08，D4/D6）。
- [ ] 6.4 固定公式前后指标口径与匹配集合，运行 raw/production 对照和 formula-only ablation，单列丢失/无效/不可评分公式及渲染资源成本（FR-06/07/08）。

## 7. M3：复杂表格内容与网格

- [x] 7.1 从获支持 backend HTML/native topology 构建 TableGrid，保存 spans、header paths、cell 内容/数学、空白/covered/missing/unreadable 状态；未知来源不猜坐标（FR-04，D5）。
- [x] 7.2 修复经验证的空 cell 丢弃/移列、单行/无表头清空、多层或重复表头身份损失；现有 dict rows 明确为版本化派生投影（FR-04）。
- [x] 7.3 添加 merged/ragged cells、多层重复表头、负号/精度/单位、脚注/cell math、无效 span 与 raw→grid→projection 保真测试（FR-04）。
- [ ] 7.4 将可追溯 cell content 与 visual_description 分离；仅在可靠 cell geometry/执行授权内允许候选重识别，native text 对齐先过既有 admission，raster 不假装有文本层（FR-04/08，D5/D6）。
- [ ] 7.5 跑 table-only ablation，联合报告官方 TEDS/TEDS_structure_only/Edit_dist 与单独命名的 cell-content 诊断，逐例区分结构、值、normalizer 和 evaluator 问题（FR-06/07，D8）。

## 8. M4 前置：生产投影、报告与迁移

- [x] 8.1 将规范顺序、inline/display 公式和 grid source mapping 接入真实 chunker、parent/row-group 及存储 payload；复用已有引用/类型，不修改检索算法，保真检查以实际消费内容为准（FR-05/08，D1/D2/D5）。
- [x] 8.2 在现有 parser bake-off JSON/Markdown 增加协议/分轨/分母/失败/性能信息和可定位错误 artifacts，保留旧 penalized/cascade 字段；旧数据新协议标 not_evaluated（FR-06，D8）。
- [x] 8.3 实现独立 document_parsing_acceptance 的 pass/fail/blocked 与预注册 actual/threshold/reason，对缺 production/held-out/完整性证据 fail closed；报告不直接发布配置或 skill（FR-07）。
- [ ] 8.4 实现 source/parser/schema/profile/normalizer 指纹驱动的 cache 失效、有界 reparse 与 parse/chunk/index snapshot 一致切换；旧/新结果不可静默混用（FR-01/05，D9）。
- [ ] 8.5 编写并验证 opt-in/rollback runbook：默认保持不变、失败回到已验证完整 snapshot、source 变化不可复用旧结果；不重引 MinerU 或新增 legacy 通用兼容层（FR-05/08）。

## 9. M4：真实路径、独立验收与交付

- [x] 9.1 跑结构/转换/安全/预算/持久化的 scoped unit tests 及架构边界检查，失败修根因后再继续，不跳过或削弱测试。
- [ ] 9.2 用真实多页 native 与 raster PDF 验证生产 composition、execution receipts、source locators、document→chunks 完整性及 single-paper/cross-source ingestion parity；fake 测试不冒充 production 成绩。
- [ ] 9.3 在同硬件/并发/cache 条件下完成冻结 dev regression 的 baseline/单项 ablations/full candidate 对照，保存分轨官方结果、失败账本、置信/切片和资源报告（FR-06/07/08）。
- [ ] 9.4 冻结候选与阈值后执行未用于调参的 document-heldout，完成既定人工复核及最小样本量检查；不足/缺失返回 blocked，不在看过结果后降低门槛（FR-07）。
- [ ] 9.5 复用既有 RAG evaluation 绑定 parse/chunk snapshot 做下游回归，不创建第二套 evaluator；运行 `python -m scripts.dev compile`、`python -m scripts.dev test`、`python -m scripts.dev smoke` 和本 change strict validation（FR-08）。
- [ ] 9.6 交付指标/失败/成本/逐例修复和未解决项、数据身份、rollback 演练与 owner 发布决议；未满足冻结门槛不标完成/不改默认；代码变更仅在范围匹配检查通过后按项目规范提交。



## 当前实施证据补记

- 固定官方 scorer 的历史 raw B0 重评分已完成：215 页，exit 0；逐指标分母、page/sample 口径与结果 SHA-256 见 `evidence/official-b0-frozen-runtime-rescore-v1.json`。这不是 2.5 的 production B1，也不是 9.3 的候选收益实验。
- 5.1/5.3/6.2 的实现与回归涵盖 typed order profile、默认保留 backend order、显式 opt-in、missing/rotated/conflict layout 退出、跨栏与唯一 owner、真实 exposed-dev Marker math/table/order fixtures、比较符不误判 HTML、多行 matrix/align 与 offsets，以及实际 chunk 投影。最新本地组合检查：`379 passed, 1 skipped, 14 deselected`，日志 `.artifacts/pdf-parsing-quality-optimization/fidelity-document-current-v1.log`。平台已有 symlink 权限 skip 不作为生产能力通过。
- 恢复 policy/预算、安全检查、cache identity 与 candidate staging 已有实现和定向测试，但部分 ROI helper 尚无真实获准 provider 调用，index port 仍缺原子 snapshot activation；4.2–4.5、7.4、8.4–8.5 不因此自动全项完成。
- 全量 `scripts.dev test` 与 `scripts.dev smoke` 尚在集成验证；早期失败必须修复并重新核验后才可提交，不能使用定向通过冒称全量通过。
