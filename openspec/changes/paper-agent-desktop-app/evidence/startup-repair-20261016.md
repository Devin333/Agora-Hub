# Windows 已安装应用启动修复（2026-10-16）

## 范围与结论

本轮仅处理用户反馈的“这个软件启动不了”，不替整个桌面 PRD、真实模型闭环或其他 OpenSpec change 核销验收。

已将本机安装的 Agora Paper Agent 从 0.1.0 原位升级为 0.1.1。真实安装后的 Electron 宿主、随包 Python/API、Node/前端和 native bridge 已连通，能够显示可操作的本机账号初始化页面。本 agent 未创建账号、未输入用户凭据、未发起付费模型调用。启动验证时保留应用运行，没有代替用户确认退出对话框；后续状态见生命周期记录。

**提交前完整 smoke 已通过：4,286 passed，23 deselected；compile、确定性 AgentLoop smoke 和 source validation 均通过。**

## 两个已复现根因

1. **未登录会话的就绪合同不完整。** 旧 BFF 在没有 cookie 时直接返回 `session: null`，跳过真实 API，缺少 readiness 所需的布尔型 `initialized`。在真实服务中 API 的 health、ready、原始 session 和前端 HTML 均已响应，但 BFF readiness 永远不满足，最终触发 `desktop_readiness_deadline`。修复为无论是否有 cookie 都查询真实 auth application API，保留 `initialized` 和本机 runtime identity，继续禁止向渲染器泄漏 session token。
2. **大体积随包资源被重复验证，放大 Windows 文件系统开销。** 旧安装包含约 79,920 个资源文件；真实调用栈反复落在每文件绝对祖先路径的 `lstat`，并存在重复全树 hash。旧宿主曾出现约 600 秒 runtime 超时及约 548 秒 readiness 超时。修复为一次操作范围内的安全目录扫描及资源摘要复用，避免逐文件重复祖先遍历和同一阶段重复全树读入。

## 安全与职责边界

- 新增 `scripts/local_desktop/safe_tree.py`，以真实 long-path `lstat` 检查节点身份和 reparse point；文件执行 lstat/open/fstat/流式 SHA256/fstat/lstat，目录执行前后身份核对，最终复核目录及根的祖先链。
- Windows 下 lstat 与 fstat 的 ctime 语义不同。跨来源比较稳定的 dev/ino/size/mtime，同来源的前后比较仍包含时间字段，新增真实出生时间与修改时间分离及变更拒绝回归。
- 新增 `desktop/src/plain-owned-path-batch.ts`，按目录批量验证 required paths，检查目录前后身份及 root canonical location。实际执行 Python 前仍有独立 interpreter 路径核验。
- 快照不跨运行或独立验证阶段缓存；未关闭 checksum、inventory、reparse 拒绝、dependency verification、afterPack 或 afterSign gate。
- 未改动 Harness 路由、quality gate、账号 owner、用户资料位置或凭据存储策略。

## 冻结与构建身份

所有代码完成后才开始统一验证。失败的 r1 保留，根因修复后的 r2 另建目录，不覆写失败证据。

- 基础 Git revision：`c4286d0c0dbb532e804af8e1d8f5386e2c17baed`；最终代码是该基础加本轮精确补丁，不能把基础 revision 当作包含全部修复的提交。
- 独立 r2 源码：`C:/Users/Administrator/AppData/Local/Temp/pa-startupfix-r2-20261016`，与共享工作区的其他未提交变更隔离。
- 冻结清单：4,420 个源码文件，`source-freeze-r2.json` SHA256 为 `8e9f53ded0b4453f9bc7d2d3683e44e5fed357efa3671486134a2ec4b6c458b1`。
- 实际 release copy 输入：2,600 个，未冻结输入为 0，构建后源码漂移为 0。
- 精确代码补丁：15 个路径（含 4 个新文件及 auth test 的单个相关块），`owned-startup-fix-r2.patch` SHA256 为 `8ffa353fd24aa1c69ce6ac13e5db46aa28d60449f28c03d9d54a4695277d9d81`。没有整份纳入共享 auth test 或其他工作流的差异。
- source digest：`3bc7821a5bde9c75b95544a409ac23deac3ee831ed965612a60c1b3897a3b963`。
- portable release：`20261015T221639Z-3bc7821a5bde-portable-860f93d1871e`。
- bundle：79,937 个资源，manifest SHA256 为 `c1f7a0e23ae791d88cb9407e5e756d31813ebb58ba196c723e6a0d59076a4547`。

第一次 bundle build 暴露 host Node/npm 与随包 Node/npm 生成的依赖树不一致，严格的 dependency verification 拒绝 4 个 extraneous WASM 包。保留失败日志，使用同一随包 Node 24.21.0 / npm 11.19.0 从 `npm ci`、source build 到 bundle 和 installer 全程重建后通过；没有删包、放宽 gate 或升级项目依赖。

## 验证结果

| 检查 | r2 结果 |
| --- | --- |
| Python 启动、bundle、release、long-path、安全扫描、runtime configuration/identity | 207 passed，3 skipped；保留平台能力相关 skip |
| desktop typecheck / build | 通过 |
| desktop unit | 24 files，164 passed |
| isolated real Electron host E2E | 1 passed，仅证明宿主及 bridge 测试范围，不冒充安装验收 |
| frontend lint / typecheck | 通过 |
| frontend unit | 173 files，1,220 passed |
| OpenSpec strict validate | 通过 |
| source release / portable bundle / Windows installer | 使用同一 managed toolchain 全部通过 |
| 必需 `python -m scripts.dev smoke` | 4,286 passed，23 deselected；compile、AgentLoop、source validation 均通过，exit code 0 |
| 精确补丁的 cached diff check | 通过 |

完整 smoke 于 2026-10-16 07:28:50（Asia/Shanghai）完成；测试部分耗时 4,816.36 秒，61 warnings。`required-smoke-r2.log` SHA256 为 `913f1cadb914b927c5984252368d22ce593ad58594f2ed91fd294462e193d43e`。最终检查绑定上述冻结 r2；不包含后续轻量化代码，也不能用于替后续候选背书。

r1 的 Python 检查曾因 Windows stat 来源语义等问题出现 28 个失败。修复根因后完整重跑上表 r2；未跳过失败测试、删断言或用 r1 局部成功冒充最终通过。

## 原位安装与真实运行证据

- 安装包：`C:/Users/Administrator/AppData/Local/Temp/pa-startupfix-r2-20261016/desktop/dist/Agora-Paper-Agent-0.1.1-x64.exe`。
- 安装包 SHA256：`f95ed8f2e1106bca67882e3bc8e74ab3c8e98ce6f35909adf4926fac7052d26b`。
- 实测 Authenticode 状态为 **NotSigned**；构建器日志的 signing 字样不代表具备有效生产签名。
- 安装完成时间：2026-10-16 06:31:45（Asia/Shanghai），installer exit code 0，实际 exe 版本 0.1.1.0。
- 实际 exe：`C:/Users/Administrator/AppData/Local/Programs/Agora Paper Agent/Agora Paper Agent.exe`。现有桌面快捷方式指向该 exe；未删除其他旧快捷方式。
- 实际页面：`http://127.0.0.1:3120/login?next=%2Fpaper-agent`，登录/创建本机账号表单存在且提交按钮可用，`initialized=false`。
- native bridge：`hostVersion=0.1.1`、`packaged=true`、`localTransportReady=true`；API health、ready、原始 session、BFF session 和 bridge 具有相同 release/config/boot identity。
- 第一次升级后物化新 release：从 host_start 到 local transport ready 为 **201,168 ms（约 3 分 21 秒）**。没有将首次激活耗时包装成快速重开证据。
- runtime phase 为 `degraded`，属于未完成远程资格配置时的能力降级，不是本机 API/前端不可达；真实远程模型、来源资格和付费调用未验证。
- 截图：`F:/github/NewsRoom/.tmp/paper-agent-startup-fix-20261016/installed-0.1.1-startup.png`。

最初观察脚本只等待 `/paper-agent`，错误地未接受合法的 `/login?next=/paper-agent` redirect。该观察结果明确标记为中止，而不是产品失败；后续通过只读连接已运行的实际应用验证了 native bridge、真实 session 和 UI，没有重新启动、输入账号或覆盖原生退出确认。

只读后续观察：宿主日志在 2026-10-16 06:46:46 记录 `runtime_stop_requested`，06:47:53 记录 `runtime_stopped` 和 `host_exit(reason=window-close)`。该退出发生在已成功验证启动之后，未归因于特定操作者；本 agent 未在后续执行自动确认或重新启动。当前不再声称窗口仍保持打开，也不把退出日志当作正常快捷方式重开证据。

后续只读观察到另一次实际安装应用启动：2026-10-16 06:59:40.295 `host_start`，07:01:40.278 `runtime_local_transport_ready`，相隔 **119,983 ms（约 2 分钟）**。相关 API/前端进程约 07:01:27 才启动，因此约 107 秒位于服务拉起之前、约 13 秒用于服务启动至就绪。该次启动并非本 observer 触发；入口是否为桌面快捷方式未核实。尚无各个校验子阶段的独立计时，并发 smoke 的性能影响也未隔离。

**性能限制仍然存在：** 不能把“已能打开”当作“启动性能已达标”，也不能把慢启动全部解释为首次升级物化。启动仍会对约 8 万个资源执行完整性/路径安全验证，多个独立阶段重新验证资源；后续性能优化应先补全阶段计时，再精简运行包和减少单次可信操作内的重复工作，不能以关闭安全 gate 换取速度。

## 资料保护与未验收项

未卸载、未清空 userData、未重置账号、未覆盖原生凭据。升级前只读记录的 2 个业务锁文件在升级后仍存在，大小与 SHA256 均一致；这只是原文件保留核对，**不是完整可恢复备份**，也不能证明所有备份/恢复场景已验收。

以下内容未在本轮验证：正常关闭后从桌面快捷方式重开、操作系统重启、真实远程模型/论文来源闭环、全部故障恢复、正式签名发行。没有据此勾选整个 M1/M4 或归档 change。

原始 receipts 和日志保留在 `F:/github/NewsRoom/.tmp/paper-agent-startup-fix-20261016/`，包含失败 r1、工具链不匹配失败、最终 r2、冻结/输入覆盖、安装、真实启动及资料保留核对；不把体积较大的二进制、运行数据和清单加入 Git。
