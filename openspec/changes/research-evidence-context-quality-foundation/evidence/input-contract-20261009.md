# 第 3 节：受控输入合同与真实模型边界验证

日期：2026-10-09。覆盖 `tasks.md` 3.1–3.17；全部本章代码及测试实现完成后集中验证，失败修复后复验，通过后进入第 4 节。

## 最终实现与证据

| 任务 | 代码与验收范围 |
| --- | --- |
| 3.1–3.3 | `ResearchInputContractService` 接受显式 profile 或自动构造；严格布尔开关与选项类型；恰好一个任务，目标和任务修订匹配；默认关闭，要求已配置 dynamic runner |
| 3.4–3.8 | 五类输入的类别/状态矩阵；canonical actor scope、paper/source/revision 校验；source 接受边界再次核对真实修订；可信 qualification port 与精确 gate 的回执核验；候选不能自升资格 |
| 3.9 | profile/item metadata 深复制冻结并纳入 checksum；prepared payload/projection 深冻结；往返序列化核对 schema 和 checksum，嵌套篡改拒绝 |
| 3.10–3.12 | 通用 PromptBuilder 只处理不透明 JSON；TaskPlan child→AgentRunner→AgentLoop 保留独立投影；普通 dependency outputs 排除保留键；首次、工具后续与 judge retry 请求验证唯一完整投影 |
| 3.13–3.14 | composition 注入已准备投影；真实 StructuredResearchCandidateWorker→OpenAICompatibleClient→离线 transport 的最终请求校验；篡改、重复、缺失、预算超限在 provider dispatch 前拒绝 |
| 3.15–3.17 | profile ID/revision/checksum、canonical tenant 和 gate 写入现有 run/trace metadata；既有 framework/Research 架构检查通过，无新增业务框架反向依赖 |

主要测试文件：`test_input_contract.py`、`test_research_input_contract.py`、`test_prepared_input_projection.py`、`test_research_prepared_input_boundary.py`（composition 与 architecture 两处）、`test_candidate_prepared_input_projection.py`，以及 `test_research_input_quality_dynamic_path.py` 的本章成功及拒绝场景。最终 provider 测试捕获真正 transport JSON body，参数化覆盖全部候选任务；模拟服务只位于测试。

## 实际检查

- 动态成功、未知 gate、非法 profile/options、跨 tenant/user/memory/paper scope 和 accepted source revision drift：16 项通过。日志 `.newsroom/quality-foundation/chapter-3-dynamic-tests.log`，JUnit `chapter-3-dynamic-tests.xml`。
- 领域/应用输入合同、完整 AgentLoop 测试、candidate worker 与最终 provider 投影、composition、完整 architecture：首轮 481 项通过、3 项失败。日志 `.newsroom/quality-foundation/chapter-3-tests.log`。
- 三项失败根因已修复：`PreparedResearchInput.from_dict` 先校验并移除 `schema` 再构造，保留 checksum 验证；两个新 composition 测试的 `AgentSpec` 补齐必填 `instructions`。
- 复验上述受影响领域/应用/composition/provider/AgentLoop projection/架构文件：135 项全部通过。日志 `.newsroom/quality-foundation/chapter-3-recheck.log`，JUnit `chapter-3-recheck.xml`。

本章通过不替代第 7 节最终全量与提交前 smoke。动态缺证路径在进入新资格 gate 之前发生的 child transcript 提交问题保留原断言及诊断，交给第 4/7 节修复，不视为“已验证 gate 拒绝”。本章未删除、跳过或降低该最终验收要求。
