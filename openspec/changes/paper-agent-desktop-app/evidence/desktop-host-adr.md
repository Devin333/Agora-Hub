# ADR：论文 App 的桌面宿主与依赖策略

日期：2026-10-14。状态：实现候选已选定；完整安装与非开发环境验收仍待完成。

## 决策

采用 Electron 作为窗口、受控 native bridge 和当前用户 App 生命周期适配层，保留真实 Next.js server/BFF、Python Research/application/Harness 运行路径。不是静态导出或 URL 快捷方式，不给 renderer Node/任意 shell/数据库权限。

选择原因是当前前端已有服务端身份、cookies、BFF 和动态 reader/workspace 路径，保持它们的语义比重写另一个 backend 清晰。宿主采用当前已完成发行并实际取得 Windows x64 binary 的精确版本；npm 元数据的最高版本本身不足以证明可用发行。

未选择直接静态封装，因为正式路由需要真实会话与 server/BFF；未在本次同时引入另一套 WebView/native 平台。没有测量不同框架体积或性能，因此本 ADR 不声称 Electron 更快、更小或优于其他框架。

## 被管理运行资源

- 安装资源根与用户资料根不重叠；用户资料、记忆、账号和恢复状态只写入当前用户的受控 data root。
- 使用独立 Windows x64 CPython 与官方 Node 资源，不把本机 Anaconda `.venv` 当作可迁移解释器。
- 完整 source release、portable bundle 和本机 release binding 分层记录实际 identity/checksums；只能重定位明确允许的本机路径，不能重写实际 package/version/hash 漂移。
- 唯一 `ReleaseManager` 计算依赖身份并执行 verify/activate；唯一 `LifecycleManager` 管理 readiness/owner/drain/stop。宿主不按端口或进程名杀服务。
- `PYTHONNOUSERSITE=1`、受控 executable、移除继承的 Python home/path；不改用户全局 PATH 或注册表解释器。

## 安全与交互合同

- renderer：sandbox、context isolation、无 Node integration；受信 canonical origin 与精确本地 App/login 路径；外链经合法协议检查后由系统浏览器打开。
- native bridge：只暴露已验证的运行状态、诊断、安全生命周期、用户主动 native 导出和有界备份/恢复操作。文件位置来自主进程对话框，renderer 不提供任意读写路径。
- 本机会话与 Research owner 保留；独立窗口不等于获得私有资料授权。
- 窗口初始及最小尺寸以真实 monitor work area（DIP）限制；不足时折叠论文工作台，不强制超过 OS 可用区。
- 退出需要最新 renderer dirty/task 状态；超时或 renderer 故障视作未知，不将旧 clean 快照当作已保存。drain 成功不代表草稿已保存。
- 恢复必须是同一个受校验备份的隔离预览与明确激活。恢复旧记忆、策略和状态有显式确认；会话/代际旋转与非终态 fenced 行为交由原 owner。

## 当前证据与未完成条件

已取得独立 Python/Node 输入；关键 native imports、Windows private DACL、独立 CLI status 和真实 memory inventory 已实测。原始输入检查与完整发布矩阵分别见相邻 evidence 文档。

仍需把同一最终 frontend/backend/config/dependency 候选做完整 source build、bundle staging、真实 App 业务窗口、native 文件导出、备份恢复、离线阅读和安装验收。仅 startup 故障窗口或 fixture E2E 不核销这些条件。真实 provider/调用预算未确认前不会执行付费验收。

签名、Windows 信任提示、实际包体与冷启动时间只能按最终输出记录，当前没有签名或性能承诺。卸载默认保留用户资料，已有目录不得被覆盖或悄悄接管。
