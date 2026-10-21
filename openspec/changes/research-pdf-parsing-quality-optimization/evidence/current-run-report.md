# PDF parsing 优化：当前实验记录

本文件记录实际完成的开发与实验，不是发布批准，也不补写历史预注册。

## 已复跑：冻结官方环境中的历史 B0

- 输入：历史 Marker raw Markdown，215 页，全部文件的运行前后 SHA-256 一致。
- 身份：`historical_backend_raw_rescore_not_production_B1`；这是 `exposed_dev`，不是独立 document-heldout。
- 官方 scorer revision：`f133a71e9e91c3621c7ce8994200a7b394a06eb3`。
- 容器：`sha256:6116ad72172e763b5c43e963d5efebf2093f2362b975f58156ce4f6c9142e617`。
- 隔离：只读根文件系统、无网络、去 capabilities、CPU/内存/PID 限制、3600 秒 wall-clock 监督。
- 执行结果：退出码 0；page matching 无 fallback/timeout，CDM 和 TEDS 无记录的 timeout/error/exception。

| 官方 page aggregate | 值 | 可评页数 | matched sample rows |
| --- | ---: | ---: | ---: |
| text `Edit_dist` ↓ | 0.1350238770 | 191 | 1555 |
| display formula `Edit_dist` ↓ | 0.4204874912 | 56 | 597 |
| display formula `CDM` ↑ | 0.6369432297 | 56 | 597 |
| table `Edit_dist` ↓ | 0.2156506151 | 97 | 123 |
| table `TEDS` ↑ | 0.7337957381 | 97 | 123 |
| table `TEDS_structure_only` ↑ | 0.8577733152 | 97 | 123 |
| reading-order `Edit_dist` ↓ | 0.2745593869 | 212 | 212 |

上表必须按各指标自己的分母解读。matched sample rows 不等于独立统计的 gold object 数量；不能把缺少该指标的页面推断为成功或 `not_applicable`。官方 sample aggregate 与 page aggregate 不同，例如 CDM 的 sample aggregate 为 0.5786448911，不能与上表 page aggregate 混比。

这些 page aggregates 复现历史 B0，不构成优化收益。逐项数值、执行监督与结果文件 SHA-256 见 `official-b0-frozen-runtime-rescore-v1.json`。完整本地 artifacts 位于 `.artifacts/pdf-parsing-quality-optimization/official-b0-frozen-runtime-v1/`。

## 生产输入身份与数据限制

- 已把 215 个已暴露页面冻结为 **215 个独立单页 PDF 来源**；不再把不同论文的页面拼成一个生产文档。source grouping 仍 unknown，不能据此宣称 document-level 去重完成。
- 官方评分 filename mapping 与 gold 信息单独保存在离线 sidecar，不送入 parser。
- 补充的 PMLR 真实论文来源共 12 篇、355 页；前两篇已明确划为 integration dev，并生成无文本层 raster 对照（native/raster 合计 4 个输入、116 页，只有 2 个独立来源组）。
- 其余 10 篇只是 prospective holdout candidates：许可证和下载内容已记录，文本/图像近重复筛查未报阈值内重复，但缺少独立 gold 和完整历史版本族核验，**还不是合格 heldout**。

## 不得越过的验收边界

1. 首次算法改动前没有冻结历史 production B1 和完整 acceptance profile。当前生产复跑只能称为 `B1_current_not_historical_preoptimization`，不能事后改名为改动前基线。
2. 不能把 raw subprocess、cached adapter replay 或序列化预测冒充真实 Harness production execution。
3. 新 profile 默认保持 `backend_preserved`；layout candidate 只能显式 opt-in。没有收益证据前不能修改默认或发布 skill。
4. 现有 index port 缺少隔离 index snapshot 与 parse/chunk/index 原子激活能力。candidate staging 可以校验，但完整 activation/rollback 仍 fail closed；不能仅切 parser 参数就宣称完成数据回滚。
5. 独立带标注 document-heldout、正式 acceptance 与默认启用均保持 **blocked**。详见 `protocol-deviation-and-current-evaluation.json` 和 `rollback-runbook.md`。


## 生产单页预检：失败证据保留

按冻结清单的第一项（不是按解析质量挑选）选择一个独立 raster PDF，当前仅用于接线预检。每次执行使用新的输出目录；失败不补写为成功，也不计入解析质量收益。

- `production-pilot-first-source-v1` / `v2`：退出码 2，composition 的 desktop-enabled 环境值错误，尚未执行 parser；已把显式值修为 settings 接受的 `1`。旧运行没有完整逐页账本，作为既有失败证据保留。
- `production-pilot-first-source-v3`：退出码 1，实际进入 composition / Harness，但没有 parser 执行证据；原错误类型被 runner 过度包装，已补保留 typed diagnostics。
- `production-pilot-first-source-v4`：退出码 1，逐 source terminal failed；`side_effect_scope_missing` / `identity_scope_ref` 表明缺少 authoritative checksum scope refs，尚未启动 Marker。授权绑定必须修在真实 Harness runtime owner，不能添加假 receipt 或绕开 preflight。
- `runtime-capability-probes-v1.json` 中的 clean-environment probes 只诊断进程路径、线程和缓存挂载能力；没有 PDF 质量结果，不能冒充上述生产预检成功。

完整本地日志保留于 `.artifacts/pdf-parsing-quality-optimization/`。以上失败尚不构成完成 215-source production rerun；后续有效执行必须另建 run identity，保留源文件、runtime source manifest、durable phases、request/receipt、ResearchDocument 和真实 chunk artifact。


### 后续单页诊断（v5–v8）

- v5/v6/v7/v8 均失败，未得到可用 production document + chunks；没有生成优化收益指标。
- v8 以只观察异常的 worker-thread tracing 捕获真实执行边界：Marker 缺少 `GoNotoCurrent-Regular.ttf`，尝试联网下载，但容器 `network=none` 正确拒绝，实际 receipt 为 `failed / process_exit_nonzero`。没有修改 request、receipt、worker result 或 gate。
- 已独立准备 offline-v2 cache：保留原 48 个模型文件，新增官方 runtime 字体，合计 49 个文件、3,462,089,484 字节；完整 manifest digest 为 `sha256:8af75fa0d0487302ffbca4a5ccec0a89261ae0b7b6112dfa113c02e231a4b27f`。原 v1 cache 未改，字体不提交、不再分发。
- 同一 image 的只读无网 probe 证实必须显式绑定 `FONT_PATH`；单设 `FONT_DIR` 不会更新独立声明的 `FONT_PATH`。这只是离线依赖准备，不是解析质量改进。
- 逐 run 文件 hash、异常诊断与新依赖来源见 `production-preflight-history-v1.json`、`offline-runtime-assets-v2.json` 和 `runtime-capability-probes-v1.json`。

### 集成检查重跑原因

旧的全量 test 和 smoke 已保存现场后中止，**不计通过**：两者均反复阻塞在 48 KiB 文本的 URI credential 脱敏正则扫描，发现无 URI 长文本的二次复杂度。将修复算法根因并保留脱敏语义，再重跑原检查；不删除用例、不缩短 fixture、不跳脱敏。取消竞态修复另有 109 项 owner 检查、17 项完整 dynamic integration 和 605 项扩展检查通过（计数有重叠），不能用定向结果冒充全量。

### 生产接线预检 v9/v10

- v9/v10 均真实完成无网 Marker 物理执行并落盘 ResearchDocument、receipt 与 chunk artifact；仍保留单页质量 gate 的 `body_too_short` / `sections_insufficient`，文档没有晋升为高质量。
- v9 的评测出口误将 durable event 的 `graph_phase_transition` 包装当成内部记录，已修 reader，按真实 activity envelopes 联验 execution identity；不在 phase 内补造字段。
- v10 继续暴露 chunk semantic hash 与完整格式化 payload fingerprint 的契约误判，出口仍为 failed。修复必须复用 artifact integrity，而非重写公式 semantic hash。详见 `production-preflight-v9.json` 与 `production-preflight-v10.json`。

### 冻结缓存转换层诊断（已完成）

- 历史 raw 到当前 ResearchDocument/只读 renderer 的 215 页转换完成；与 production run 分轨，不运行 parser/Harness、不冒充 downstream projection。官方 scorer 已在同一冻结 image/commit/config 内完成，退出码 0；同一冻结 image/commit/config 的 page matching、CDM、TEDS 均无 timeout/error/exception。
- 当前实现转换 215 页：214 页 `partial`、1 页显式 `missing_output`。batch 003/008 全部恢复为可转换；batch 004 有 24 页 `partial`，另有 1 页仅含不可用空 Marker equation，按 `missing_output` 计入分母。没有 raw fallback 或空页剔除。
- v1 转换耗时约 5.10 秒、v3 转换耗时约 5.67497 秒，均不是 parser latency；215 页均是 exposed dev，不能据此确认优化收益、独立验收或默认策略晋升。


## 当前实现重跑：冻结转换与官方指标

- 转换目录：`.artifacts/pdf-parsing-quality-optimization/cached-conversion-frozen-runtime-v3/`。
- 轨道：historical raw -> 当前 `ResearchDocument`/只读 renderer 的 `cached_artifact_diagnostic`；`parser_rerun=false`、`harness_execution=false`、`heldout=false`、`downstream_projection_captured=false`。
- 215 页全部到达 terminal；214 页为 `partial`，1 页为显式 `missing_output`。空 Marker equation 被保留为 unavailable block；不可靠 table cell math 被保留为 unreadable cell；不补造文本或几何。
- 官方 scorer：同一 frozen scorer image、commit `f133a71e9e91c3621c7ce8994200a7b394a06eb3`、config，v4 execution exit code 0，wall time 1376.12 秒。

| 指标与聚合口径 | 当前 v3 | 缓存 v1 | v3 - v1 |
| --- | ---: | ---: | ---: |
| text `Edit_dist.ALL_page_avg` ↓ | 0.0402532495 | 0.3313078279 | -0.2910545784 |
| display formula `Edit_dist.ALL_page_avg` ↓ | 0.2115688812 | 0.2616290850 | -0.0500602039 |
| display formula `CDM.all`（sample）↑ | 0.8948442211 | 0.8832730318 | +0.0115711893 |
| table `Edit_dist.ALL_page_avg` ↓ | 0.1959005656 | 0.6319567138 | -0.4360561482 |
| table `TEDS.all`（sample）↑ | 0.7505984527 | 0.3939807745 | +0.3566176782 |
| table `TEDS_structure_only.all`（sample）↑ | 0.8319139045 | 0.4255336277 | +0.4063802768 |
| reading order `Edit_dist.ALL_page_avg` ↓ | 0.1861070850 | 0.4202397762 | -0.2341326912 |

可评页数：text 191、formula 56、table 97、reading order 212；两次 page 集合相同。matched rows：text 1555 -> 1554，formula 597 -> 597，table 123 -> 123，order 212 -> 212。215 是 requested/terminal 分母，不是每项质量指标的分母；missing_output 页保留于账本和输入中，但在这四类官方 matched rows 均无记录，不能声称其向每项质量指标贡献了失败分。

以上是同一 exposed dev/frozen historical 输入的诊断变化；不能解释为独立 baseline gain、生产质量或 held-out 结果。

## 生产 composition 单页重跑：v14

- run：`pdf-current-dev-pilot-1-v14`，1 个 raster PDF，真实 Marker Docker execution，退出码 0。
- 已验证：source hash、durable request/receipt、Harness `PLAN -> EXECUTE -> VERIFY`、ResearchDocument 持久化和 chunk payload 持久化。
- 结果：`ResearchDocument.status=degraded`，`fidelity_status=validated`，`qualityScore=0.5795`，质量原因包括 `body_too_short`、`sections_insufficient`，生成 5 个 chunks；chunk activation 另因 `index_snapshot_atomic_activation_unavailable` 保持 `blocked`。
- 结论：生产接线、receipt 和下游完整性证据已补齐，但质量 gate 未通过，不能计入质量收益、不能晋升默认配置。

## 验收结论

本轮修复解决了已证实的局部缺失导致 document-wide fidelity 不可用的根因，并完成了缓存转换的官方复评；生产试跑证明真实路径可执行，文档质量未通过；activation 另因原子 index snapshot 能力缺失保持 blocked。正式 B1、ablation、document-heldout、downstream 回归和默认启用仍为 **blocked**，未修改默认策略。


## 代码验收范围与集成修复

最终代码验收针对基于 `ce7584f3de6082ddda45202ee37b4fae13557402` 的选择性提交版本，在独立 worktree 中执行；不把工作区其它 desktop、RAG protocol 或 MinerU removal 改动混入本次提交。官方 scorer 的 caller inventory 仅登记为 `blocked / follow-up-required`，不授予生产执行资格。

集成检查中已定位并修复以下根因，未降低预算、删除断言或跳过失败测试：

- Harness：取消已排队 child 不等于其 spawn 已持久化确认；未审计的 handle/reservation 保留用于恢复，cohort fence 阻止迟到 callback 继续执行。
- 执行证据：先传播未确认终止状态，再发布 evidence；publish 失败不抹去 indeterminate。移除无法绑定原始 request/output 的 replay shortcut，replay 上下文仍禁止 live execution。不可用 provider 在命令硬件检查前返回明确不可用诊断；available provider 仍需全部 policy 和 registry admission。
- 运行成本：复用不可变默认 event-schema 注册，不缓存用户 validator/upcaster 的安全判定；长文本 URI credential 脱敏使用线性扫描，不截断原始文本。
- 存储安全与依赖：Windows dangling symlink/reparse 在路径解析和 SQLite 连接前拒绝；低层 JSON storage 不再 eager-load 无关 Redis/auth 依赖，保留 package public exports。
- 并发身份：catalog projection 的 stale identity write 曾覆盖另一线程追加的 source snapshots；现于同一文件事务中合并追加型 provenance，再由既有 domain model 校验规范化，不把业务去重规则搬入 infrastructure。
- 候选预算：reader repair 的估算输入包含 response schema；仅去除 validation-neutral schema 注解，估算请求由 9,510 降至 7,737 tokens，原 8,192 上限不变。完整 prepared envelope、业务字段、对象字面量、required/refs/additionalProperties 约束均保留。

修复后的组合 owner 回归为 `240 passed, 1 skipped`；该 skip 为既有平台能力限制，不能当作生产能力已验证。完整 compile、两项 OpenSpec strict、architecture、test、smoke 的最终状态以后续 `final-code-validation-v1.json` 为准；定向通过不代替全量门禁。

各实验的指标对应各自记录的 frozen runtime source manifest；后续安全/并发/依赖修复不回填历史 scorer 或生产 receipt，也不把它们宣称为最终提交字节上的完整生产重跑。正式 B1、独立 held-out、消融、下游 snapshot 回归和发布激活仍保持 blocked。
