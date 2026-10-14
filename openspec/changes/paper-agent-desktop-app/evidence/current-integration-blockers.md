# 当前候选的真实集成阻塞记录

日期：2026-10-14。以下均为当前 worktree 的复核，不是最终 release 通过证明。

## 已复跑检查

- 必需 `python -m scripts.dev smoke`：进程退出码 0；pytest `4526 passed, 23 deselected, 61 warnings in 4494.49s`；offline AgentLoop fixture 成功且 `network_calls=0`；来源 `is_valid=true`、error/warning 均 0。
- Python desktop config/runtime/runtime-config/credentials/protocol/bundle/release-config/memory-recovery/processes：`106 passed in 7.58s`。
- native host：TypeScript 通过；14 files、51 tests 通过。新增配置桥等行为仍需检查覆盖和真实宿主验证，不能由此核销全部 native 合同。
- 正式论文页面浏览器 fixture 回归：14 passed。其网络路由使用测试 fixture，并非真实论文服务/安装验收。

## B-01：ReleaseManager 长路径误报（已实测）

- 正常 build 在独立资料根 `C:/Users/Administrator/AppData/Local/Temp/AgoraPaperAgentCandidate-20261014-1653` 失败，code=`desktop_release_resource_missing`。
- 候选：`20261014T085434Z-e87b23830dba`；该候选没有 active pointer，不能启动为已通过 release。
- 失败资源是 Next.js 自带 `react-server-dom-turbopack-client.browser.development.js`。原始绝对路径长度 261；普通 `Path.is_file()` 为 false，内部 Win32 extended-length 形式为 true；实际内容 size 与 SHA-256 均与 manifest 相同。
- 根因定位：`scripts/local_desktop/release.py` 的 verify 资源循环仍使用普通 `resource.is_file()` / `resource.read_bytes()`，与 bundle 输入复制的 extended-length 支持未贯通。
- 不删除资源、不篡改 manifest、不放宽 inventory/checksum、不修改 Windows 系统设置。应在所有相关内部可信 IO 保持 lexical no-reparse/containment 验证后使用一致的长路径策略，并加真实 >260 字符发布目录回归。
- 另启短目录候选仅继续验证独立业务流程，不构成长路径修复或最终发布条件通过。

## B-02：降级但本地服务健康时的宿主入口（代码审阅，待真实复现）

- 旧中间候选的 start 回执 `ok=true/state=DEGRADED`，api/frontend/bff/raw_rewrite/runtime health 均 true。
- 当前 `RuntimeCoordinator` 将 DEGRADED 映射 degraded，`navigateWhenReady` 仅接受 ready；未配置远程模型时可能停留故障/待配置页，不能进入应当可用的本地阅读。
- 不能简单把所有 DEGRADED 当 READY，因为 DRAINING/身份或 transport 异常同样可能被映射为 degraded。需要 owner 提供可验证的本地就绪语义并保留远程能力未验证/不可用提示。

## B-03：API-only secret 投影的真实子进程边界（代码审阅，待修复验证）

- 当前 start 在 common/frontend environment 移除固定 key 后，仅向 API `_launch` 参数注入 key。
- 但 `_launch` 又把同一个 environment 传给两个 log collector 子进程。现有 API-only 测试 monkeypatch 整个 `_launch`，没有覆盖这条真实分支；不能据其通过声称“仅 API 子进程”。
- 应在可信 API 进程的最终 spawn 边界才注入；log collector/frontend/native 公共进程不得得到 key，且不更改全局 os.environ。

## 执行约束

原指定代码 worker 多次收到上游 403 `insufficient balance`，已停止相应工作，没有静默换模型。主控仍可进行本机只读审阅、测试与真实构建取证；未完成代码不提交为生产完成项。真实模型/搜索付费验收仍需用户确认 provider 与预算，两者是独立约束。

## B-02 补充：真实 owner 首启已复现更早的拒绝

短目录 source build 由唯一 owner 成功构建和激活：release `20261014T085734Z-e87b23830dba`，source digest `e87b23830dba46e1445205e60f760cfc1425401f1467d4beab918147259d26ad`。这仅是中间检查点，不是完成/安装证明。

随后同一 owner 的正常 start 返回 `ok=false/state=ERROR/code=desktop_doctor_blocked_start`，未启动服务。doctor 的 profile/Python/Node/frontend lock/production release/ports 检查均通过；本地 parser/storage 已配置，远程 analysis/QA `configured=no`。当前 `_DesktopAppDoctor` 的未配置远程模型被 aggregate 当作 fatal required 条件，LifecycleManager 随之拒绝全部本机服务。它与 PRD “未配置联网仍可本地阅读/记忆管理”的合同冲突，而且会使首次用户无法进入正式页面录入配置。不能放入假 key 或放宽全部 doctor 错误来绕过；需要区分本地 startup admission 与远程能力可用性，保留真实权限、端口、release/storage 错误的 fail-closed。

因此此次没有真实登录/PDF导入/本地记忆验收结论。已关闭隔离浏览器，没有注入 cookie、手工启动 child 或绕过 gate。当前另一个 portable staging 进程仍有独立取证价值，但其产出也不能核销上述首启/长路径阻塞。

## B-04：新增 native bridge 的真实 E2E 合同尚未同步（已复跑）

`npm run test:e2e` 已重新构建 host 并实际启动 Electron，返回 1 failed。首个断言失败于 bridge 的 exact key contract：旧 E2E 只允许 export/lifecycle/runtime/version，当前生产 bridge 已新增 config/credentials。不能把 exact 断言改成宽松 includes 或跳过用例；需要准确验证新两个 namespace 的每个允许方法与拒绝参数/来源行为，并继续执行后续单实例与安全路径断言。当前没有这一轮 Electron E2E 通过结论，此前故障窗口通过记录不覆盖新增配置桥。

## B-05：portable staging 的 npm 工具版本一致性（已实测）

portable staging 使用已校验 Node v24.21.0 / npm 11.19.0 后被 `desktop_node_dependency_tree_invalid` 拒绝，4 个 problems。主控使用同一 managed npm 在原 source release 上进行只读 `npm ls --json --all` 复现：`@img/sharp-wasm32@0.35.5`、`@napi-rs/wasm-runtime@1.1.4`、`@tybys/wasm-util@0.10.2`、sharp 子目录 `@emnapi/runtime@1.11.3` 被判定为 extraneous。即使 npm exit 0，owner 仍正确拒绝，不篡改 dependency digest 或忽略 problems。

原 source build 使用本机 Anaconda 的 Node/npm，portable 输入为另一版本。下一候选 source build 已在**仅当前命令进程的 PATH**前置已管理 Node/npm，保持原唯一 ReleaseManager 正常 build，尝试得到与打包输入一致的 dependency tree；不改系统 PATH、不改 package lock、不删除包。该进程尚在运行，不能预先宣称修复或 staging 通过。
