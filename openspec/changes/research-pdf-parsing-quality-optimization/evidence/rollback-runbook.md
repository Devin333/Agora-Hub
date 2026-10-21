# PDF parsing profile opt-in 与回滚操作手册

状态：**草案 / 未获准生产激活**
变更：`research-pdf-parsing-quality-optimization`
最近复核：2026-10-22

## 安全决策

当前 fidelity adapter、parser quality gates 和 chunk source maps 是实现候选，不是已验证的发布 snapshot。现有证据缺少正式冻结的 production-document B1、独立 document-heldout、已完成的回滚演练，以及原子 parse/chunk/index 指针切换证明。因此：

- 不得将候选 parser/profile 晋升为生产默认。
- 不得将已暴露的 215 页 OmniDocBench 或人工 25 页批次冒充 held-out、真实多页论文或完整生产证据。
- 不得只切换 parser 设置，却保留来自其他 profile 的 document、chunk manifest 或 index artifacts。
- 保持最后一个已知可靠的完整 snapshot 及其配置不变。无法确认其精确身份时停止 rollout，不从 `latest`、时间戳或可变 alias 推断。

## 必需的发布记录

任何获准的 opt-in 验证或 rollout 前，均须在不可变 run manifest 中记录：

1. Repository revision、dirty-content digest，以及 parser/profile/schema/normalizer/chunker 指纹。
2. 不可变 Marker image digest、weights/config 身份，不能只有可变 image tag。
3. Source document 身份、SHA-256、scope/tenant，以及 exposure/split 分配。
4. Baseline 与 candidate document ID、artifact refs/checksums、parse run ID 和 execution receipts。
5. 与每个 document snapshot 匹配的 chunk manifest ID/checksum 和 index namespace/snapshot ID/checksum。
6. 确定性质量报告、requested/terminal 页数、失败、时延/资源和 acceptance-profile 指纹。
7. 待恢复的完整 previous-good snapshot，以及批准操作的 operator/owner。

任何身份缺失，或 document/chunk/index 元组中的身份不一致，都不具备激活资格。

## Opt-in 操作流程（尚未端到端验证）

1. 确认 acceptance profile 在候选实验前冻结，数据满足所声明的 exposure 条件。
2. 使用隔离 evaluation namespace 和既有 Research composition/execution adapter。记录实际生效的 `NEWS_RESEARCH_PARSER_BACKENDS` 和 resolved execution profile；直接 Marker subprocess 不能算作生产证据。
3. 生成 candidate document、chunk manifest 和 index snapshot，不替换 active references。对这些精确 snapshot ID 运行确定性 parsing acceptance 和既有下游 RAG evaluation。
4. 要求具备已验证的完整 snapshot 元组原子 compare-and-swap/promotion 操作。**本变更尚未验证此能力；在实现并测试之前，不得进入第 5 步。**
5. 仅在确定性 gate 通过且指定人工 owner 记录批准后晋升。报告或模型输出不能授权 promotion。

## 回滚流程

1. 停止 candidate ingestion/promotion，并阻止向 candidate namespace 继续写入。
2. 解析已记录的 previous-good 元组，包括 parser/profile config、document artifact、chunk manifest 和 index snapshot。恢复 references 前核验全部 checksum 和 source hash。
3. 使用已验证的 snapshot-switch 机制原子恢复完整元组。不得只恢复 parser 设置或 document artifact。
4. 重新读取 active references，断言其均指向同一已记录 snapshot lineage。运行 scoped parse/read/chunk/retrieval smoke，并对比 source hash、profile fingerprint、document ID、chunk manifest ID 和 index snapshot ID。
5. 记录候选失败原因、receipts、切换前后 references、operator 和验证输出。保留 candidate artifacts 供诊断，不覆盖历史证据。
6. Previous-good 元组缺失、损坏或无法原子激活时，停止受影响 scope 的 ingestion 并隔离 candidate；不得从混合 snapshots 拼出 fallback，须交由 storage/index owner 处理。

## 当前验证缺口

- 尚无正式 production B1 或具备独立资格的 document-heldout。单页 v14 pilot 仅证明真实执行与落盘路径可运行，其文档质量仍为 `degraded`。
- 尚未验证端到端原子 document/chunk/index promotion 和 rollback API；v14 activation 的实际阻塞原因是 `index_snapshot_atomic_activation_unavailable`。
- 尚未执行回滚演练；本文件是安全操作契约，不是成功演练证据。
- Runtime parser 设置只能选择 backend，不能单独构成完整 snapshot 回滚机制。

所有缺口获得权威证据且回滚演练成功前，本手册必须保持 **未获准生产激活** 状态。
