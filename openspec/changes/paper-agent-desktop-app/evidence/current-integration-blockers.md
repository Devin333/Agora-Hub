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

### B-05 后续：同工具链构建与真实 portable stage 通过

已用命令进程级 PATH 前置 managed Node/npm，由唯一 owner 完成 source build：`20261014T090824Z-e87b23830dba`，dependency digest `3d034553cd31caa50745e5857e5b2ace3201d052946895ee0393fb5b2c9e84c5`。同 managed npm 的完整 `ls --json --all` 返回 0 且无 problems；未修改 package lock 或删包。

随后正常 portable staging 退出码 0，bundle `desktop-13fb2b551f22afdee74631cb`，79,949 个资源、1,935,863,691 bytes；CPython 3.12.14 / 85 distributions、Node v24.21.0 / npm 11.19.0。portable dependency digest `860f93d1871e3ee5dab24c10452c6b07189c5cc053d4e71be93500223e1bde0d`，输入 source identity 与新 runtime identity 均记录在 envelope，未伪造其相同。

这证明构建输入需要同工具链，不证明当前 doctor、长路径、密钥子进程或正式 App 已完成修复。该 bundle 仍是中间候选，已在独立根运行 portable binding 和 Node envelope 全资源校验，结果尚待取得。代码 worker 此后重新分派已可实际执行，按不重叠 scope 收口上述问题；此前余额错误不再是当前唯一执行阻塞。

### Portable 入口取证的命令修正（非放宽 inventory）

首次 portable CLI 直接使用 `python -s -m`，未在 import 前提供 `-B` 或 `PYTHONDONTWRITEBYTECODE=1`，因而在受校验 payload 中生成 `scripts/desktop_app/__pycache__` 的三个 .pyc。Node envelope 验证和 Python binding 都实际拒绝 `missing=0/extra=3`。这是主控验收启动方式错误，不应忽略 pyc 或把 inventory gate 失败当通过。

已等待第一次 binding 进程实际终态退出 2，按 manifest 核对只有上述三个精确非登记 .pyc 后，以经绝对路径 containment 和 no-reparse 检查的 `Remove-Item -LiteralPath` 删除它们；没有递归删除或修改任何登记资源。随后以 `-B -s` 和命令进程级 `PYTHONDONTWRITEBYTECODE=1` 重跑 owner binding，并重新运行 Node 全量校验。两者仍在执行，结果待取得。实际宿主 RuntimeClient 的入口也必须在首次 import 前禁用 bytecode，已要求其 owner 验证并加回归；不可依赖 CLI import 后才投影环境来保护不可变 payload。

### Portable 校验结果与 dependency identity 表示根因

禁用 bytecode 后 Node `validate:runtime` 全资源/envelope 校验退出 0。Python binding 则继续 fail-closed，返回 `desktop_app_release_dependency_drift`（Python dependency）。主控在同一个 portable interpreter 上只读对账：expected/actual 均 CPython 3.12.14、85 distributions；packages 的 JSON 序列完全相同，但 manifest entries 是 list，`ReleaseManager.dependency_identity` 的内存 entries 为 tuple，raw dict 比较为 false。

这不是允许忽略版本或 packages 的依据。已交 release owner 将两个 dependency identity producer 统一为 JSON-native list-of-list 稳定模型并补真实序列化 roundtrip 回归；bundle 必须继续精确验证包/版本/hash。修复后需新候选冻结并重新 binding，不能直接重写旧 manifest。

同时已完成的当前源码 root-fix checkpoint：App 显式启动 capability policy 区分本地硬门和未配置远程能力；实际 spawn 边界只给 API 传 fixed key，不给4个log collector/frontend/common/global。主控复跑匹配3组 `54 passed in 3.75s`。仍须新 release 正常 start 才能证明真实首启修复，不核销 AC。

## 新候选真实首启与认证合同取证

包含首启 policy/disabled-route/key-isolation/长路径修复的 source candidate 已正常 build：`20261014T103935Z-f881824e16d5`，source digest `f881824e16d55378b42a4919931e3120b8c19df6ee49b912a0ca8972bfe4f023`。正常 owner start 返回 `ok=true/DEGRADED` 且 api/frontend/bff/raw_rewrite/runtime 全 true，未配置远程模型没有再次阻止本机服务启动。这仍不是安装验收。

通过实际生产 BFF 首次 bootstrap 返回 200，实际 session GET 200、HttpOnly 本机会话存在；未注入 cookie/token。由此发现两项此前 fake tests 未覆盖的问题：

1. 本地账号 bootstrap/login 成功后，root PortalAccountProvider 仍保留 guest context，直到手工 reload。已将成功 auth 状态通知放入统一 event owner，失败不广播，Provider abort stale initial fetch；真实 API→Provider→history owner 回归已补。正常 reload 仅用于继续定位，不当作此问题通过。
2. reload 后 history/desktop identity/capabilities 均 500。真实 `AuthSessionResult` 为扁平 `sessionId`，认证 middleware 却读取 `session.session.sessionId`；原 fake 恰好伪造该嵌套结构掩盖错误。已根因修为 `session.sessionId`，fake 改遵循真实模型，并增加真实 Auth + disabled Research + History/Desktop/Memory 集成测试。没有放宽认证或返回假空库。

页面还补齐初次 history 失败的显式错误和原 owner 路径 retry，不再无限 loading 或借访客 workspace 冒充成功。

主控已关闭浏览器页面并通过唯一 owner 完成 drain/STOPPED，保留隔离账号和资料根用于正常重开复核。新源码已冻结并在同一根重建；重建/真实导入仍待结果。当前全前端复跑 `164 files / 885 tests passed`，typecheck 通过；原生 E2E 故障窗口与 strict bridge contract 已重获 1/1 pass，但未当作真实论文窗口证明。

### 本轮真实导入前的工作区写入阻塞

修复后的生产候选 `20261014T113432Z-5bc0e8b23226` 已正常首启；保留原隔离会话重开时，页面显示真实 owner `user_c37b37efb4381350` 与 0 篇空库，GET history/identity/personal-memory policy 均 200，未出现六篇样本。

通过真实页面明确选择官方公开 PDF 时，请求身份需要先保存至 workspace。浏览器捕获 `PUT /api/research/history` 为 403，尚未发送 import POST；页面保留失败状态，不报导入成功。本 BFF PUT 已验证浏览器 same-origin，但 upstream headers 没有 server-owned canonical Origin，与 local-desktop 写入认证合同不一致；已分派精确根因修复及 foreign-origin/default-profile/no-store/CAS 回归。不会通过取消 backend Origin gate 或手工调用导入绕过请求身份持久化。

本轮 smoke handle 已确认正常结束：exit 0、`4526 passed, 23 deselected`、offline AgentLoop `network_calls=0`、来源校验无 errors/warnings。该运行启动后发生认证修复，虽有匹配认证集成 `18 passed`，最终 release 仍需重新绑定最终检查范围，不声称该 smoke 是所有后续改动的最终 gate。已独立提交 flat session 合同根修复 `a2abb93f`，未混入其他 task 的代码。

新的 portable staging `desktop-4a02600e9622cf14e5d765de` 已成功；同 managed runtime 的正常 binding 仍在执行，等待最终状态，未直接修改此 payload 或忽略 inventory。


## 2026-10-14 22:16 集成检查点：Origin、首次绑定与本地记忆基础

- History PUT 与 guided mutation 在浏览器 same-origin 验证之后，通过共享 server-owned helper 仅为 local-desktop 注入 canonical Origin；foreign browser Origin 在 upstream 前拒绝，default profile 不合成 Origin。前端 owner 记录：4 files / 19 tests、全前端 165 files / 889 tests、typecheck/lint/production build 通过。修复后的真实 PDF UI 导入尚未验收，不能以代理单测替代。
- Portable 首次绑定现在通过唯一 `ReleaseManager.configuration_identity_for_runtime(stage)` 计算最终 config，而非手工复制 source config。真实集成新增 configured→null 回归进一步暴露旧 model 路径已删除但 identity calculator 仍先读取它；bound-null 分支已在 dependency calculator 前移除两个 model 引用，保留 source 真实 size/hash 检查。普通未绑定 profile 分支不变。
- 主控使用项目 `.venv/Scripts/python.exe` 复跑 bundle/config/runtime_config/release/runtime/CLI/credential 检查：`93 passed in 11.99s`；新 source release `20261014T141041Z-056b8ae19dd4` 正常构建并激活，source digest `056b8ae19dd4cb346b72a68dcef61098a58466631607feb22d6d3559fb2299d7`，config digest `327cdd57bc89bb9aae387c3e4a04d7ee04860a414c69551d1609015c75c24b8a`。这仍是中间 source candidate，不是最终 installer。
- `6.1` 的基础实现已独立提交 `a2805e6e`：domain/application/port/SQLite adapter 分层，owner namespace、来源、revision、candidate/active/disabled/deleted、默认拒绝远程处理以及本机确定性检索。主控使用项目 interpreter 复跑三组 core 测试：`22 passed, 1 skipped in 2.95s`。跳过项是当前 Windows 无 symlink 创建特权的负向用例；不把该项当作已通过。仅勾选基础存储任务 6.1；6.2–6.5 和完整 AC-09–AC-11 仍需 API/UI/重开/外发证据。
- 观察到正常无 cookie 首启已有 proxy → `/login?next=/paper-agent`。额外修复的是失效 cookie/挂载后退出后的 client AuthGate，避免打开只支持外部账号的 Portal dialog；default profile 保持原行为，3 files / 22 tests 通过。该修改晚于 source candidate e snapshot，下一冻结必须重新构建，不冒充已在 e 中验证。
- 本轮完整 `python -m scripts.dev smoke` 有 live handle（6998，C:\Python314\python.exe），输出已进入全范围 pytest，尚未终态；不会因慢或观察超时中止/重启，也不把旧 smoke 结果当作此轮通过。匹配生产 runtime 检查使用项目 `.venv`，二者 interpreter 范围明确区分。
