# 上下文连续性触发核查

核查日期：2026-10-08；清单编号及交付说明更新：2026-10-10。

## 当前决定

P2 上下文连续性保持未实现、未启用。触发要求、样本核查和“证据不足，暂不启动”的决定已完成，对应细化后的 6.3–6.5；连续性实现任务 6.6–6.12 属于条件待办，前提尚未满足。

本核查不证明生产环境没有上下文丢失问题，只说明目前可读取的本地证据不足以建立合格触发。第 4/5 节的离线或 fake transport 回归不能被当作真实生产窗口损失证据。

## 已读取的证据

核查读取以下本地目录下 119 份持久化 JSON/JSONL 记录：

- `.newsroom/research`
- `.newsroom/guided-verification`
- `.newsroom/verification`

所有文件均解析成功。核查统计顶层记录与 event type，并查找包含 context、compaction、rollover、condition drift 或 repeated read 的事件名称。样本中有 70 个 `research_parse_phase` 和 15 个 `research_parse_terminal` 事件，没有找到合格的实际模型窗口触发事件。来源文档或 artifact 存在本身不能证明已接受证据丢失、条件漂移或重复读取成本。

本地生成记录为 `.newsroom/quality-foundation/context-trigger-audit.json`，包含每个文件路径和 SHA-256。排序后的文件 manifest checksum 为 `5363d1521bb7b85e2f3c7097b926c364094eb38881df06c25514b5644c87e643`；报告 checksum 为 `612358fa4bffba6e46860a9371c9ef1907d09c744c8a5917dcfe3bbd3fe09788`。这些身份只定位本地核查样本，不能替代生产 run 的 durable receipt。

既有 Research context 夹具证明了受保护输入超窗口拒绝、已核验摘要重放；它们未证明已接受 Research 证据或条件实际丢失，不能据此提前启动连续性代码。

## 后续触发记录的要求

执行 6.6、进入连续性实现前，必须记录一个可复现的受影响真实 run，或能代表该实际问题的固定案例，至少包含：

1. run、tenant、paper、task、source revision、context policy 及受测代码的身份。
2. 实际 model-window boundary，以及窗口前后 durable context/transcript/artifact 引用与 checksum。
3. 至少一项可测问题：边界后缺失已接受 evidence ref/条件；条件与已接受来源不一致；或对相同 source revision 的重复读取次数、token/latency 成本超过接受的测量基线。
4. 测量方法、受保护内容、实际结果与负责人的判断，说明问题需要限定单 run 的解决方案。

仅有长对话、worker 自报上下文丢失，或 observation-only 的 `insufficient_verified_progress`，都不是充分触发。现有 Harness gate、retry/replan 上限和发布权限继续有效。满足触发后的实现必须复用现有 context assembly 与 transcript/artifact/event，不建立平行状态库、archive、CAS 或跨 run 记忆。