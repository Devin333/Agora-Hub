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


### 2026-10-14 接续：本地解析根因已修复，等待新生产候选

Candidate E 中实际导入返回 `research_parse_unavailable`：disabled LLM 触发完整 settings 异常，整个 service 被置为 unavailable。Candidate F 已用独立 local settings/composition 修复这条控制路径；真实认证+持久化 PDF integration、无网络断言、guided-disabled 安全边界均通过。只有用户已选文件进入本地 parser，远程模型和 source 没有被重新开启。正式 UI 单色导入和失效回执展示也已集成并通过回归。

Candidate F 尚不能核销发布：source build 曾自然 exit 134；诊断证实 Node heap exhaustion。已精确清理本任务仅设计预览的临时服务释放资源，再通过唯一 owner 重建。旧完整 smoke 仍 live，但早于本次 source freeze，不能当作最终 gate。新 source build、生产 BFF 的真实 PDF/阅读/笔记重开、native business window、安装与真实 provider/预算确认等条件继续保留，不以局部测试或 parser preflight 代替。


### 2026-10-15：导入已成功，发布仍保留 reader 与 gate 条件

Candidate F 已经真实 import POST 200 / passed 和 reader GET 200；但 reader 因未知发布日期崩溃，不能核销可阅读。日期 formatter 与 owner 文件名标题来源已做 root fix 并通过定向检查。Candidate G 正在正常重建并执行新完整 smoke。原 smoke 的 4 个失败保留；资源清理后同原 interpreter 四项独立通过以及完整相关文件 28 项通过，均不能取代新全范围门禁。安装、原生业务窗口、重开/离线/恢复与用户确认的真实 provider/费用闭环继续待验收。


### B-06：Portable Doctor 的真实 Windows 长路径重复检查

Candidate G 79,960-file bundle 已通过全部 Node checksum 和完整 ReleaseManager.verify，但普通 Doctor 在 266 字符资源路径上 is_file/read_bytes 失败，错误被投影为 active_release_invalid；extended-length IO 同文件真实存在且 size/hash 相符。必须精准修复 Doctor 内部 IO，保持全量 gate、安全 path/junction checks 和 identity 正常路径，不能删包资源或缩短用户资料根回避。managed startup/笔记重开/安装因此尚未通过。source BFF 已验证真实全文可读，不能替代 portable/native proof。

### B-07：Portable 重绑定后的 raw API rewrite 仍冻结源端口

Candidate H managed start 已自然 exit 2：`desktop_readiness_deadline`，API/frontend/BFF/runtime 均通过，raw_rewrite 未通过。真实 bound `.next/routes-manifest.json` 仍将 `/api/v1/:path*` 发到 source build 的 `8122`；本实例真实 API 为 `8124`。长路径 gate 已通过至真实启动，不能把新的路由失败归回旧 Doctor。必须修复 local-desktop 的 server-owned runtime 路由绑定，保留原始 API、owner/auth/Origin 和 readiness 四项检查；不得手改不可变 manifest、删除 raw probe 或固定用户端口回避。H 的真实笔记/记忆 UI helper 尚未运行，未虚构回执。该 root fix 将进入下一 Candidate I。

### B-08：真实 PDF viewer transport 仍经公共 URL proxy

J development native 重验已通过启动、普通账号初始化、文件选择、真实解析、server-confirmed library 和未知日期 reader；进入 PDF 模式后实际 GET `/api/papers/pdf` 400，fallback 如实显示加载失败。后端提供的是 owner/revision/fingerprint 绑定的相对私有 source URL，而 shared viewer 总把它封装进仅接受公共绝对 URL 的 proxy，因此根因在 transport 接线，不是用例 timeout。必须在严格私有路径校验后直连同源受控 route，继续由后端认证与 source 身份判定；公共代理 allowlist/SSRF 不放宽。此修复将使 J 源漂移，J 历史失败与不可变包保留，下一 K 只在完整 UI 闭环确认后进入最终门禁。

J 第一次 native helper 另有两处 PRIVATE 观察错误：把正常 DEGRADED→应用路由过渡当终态，以及 Playwright launcher cmd.exe PID 当实际 Electron PID。v2 已按真实 Electron PID及严格 local transport 状态修正，没有改生产就绪判定；原失败回执未回写通过。真实原生退出确认由已校验 exact owned dialog/button 点击，durable owner 日志均自然 stopped→host_exit。J v2 在 PDF 失败之后正常 owner drain 也真实完成，仍不核销整体业务。

### 2026-10-15 03:05：冻结 K 原生真实 PDF 通过，完整验收仍未通过

- K source release `20261014T185240Z-3c1c3993b69d`，source digest `3c1c3993b69d92dfa02a9c3999e093beda8c20034093658b22d750888d6fe642`；全前端 172 文件 / 1041 项、普通与显式 feature TypeScript、lint 全部通过。
- K development Electron 经正常账号 UI 和真实文件选择器验证：私有 PDF 原件同源传输成功，真实 18 页、第 2 页 canvas 595×841；刷新恢复 PDF mode / page 2，笔记精确匹配 owner 工作区回执且重开保持。黑白真实窗口截图已保存，无 pageerror、console error 或外网 browser request。
- 整轮 native helper 仍 exit 1：多操作组合的个人记忆步骤超过 15 秒总体预算，实际 create/confirm/disable POST 均 200，随后定位/刷新过程尚待独立诊断；不得据此认定完整记忆 native 验收通过。失败 receipt 和截图保留。原生 owner 已确认正常 drain / stopped / host_exit，未 fallback kill。
- 完整 portable K bundle `desktop-9c4994dd76dcacdfc369d984` 已 staged。J portable 已正常 drain stop 并保留数据；K 使用独立数据根开始实际 3124/8124 重绑定。首次 CLI timeout=600 正确被有限预算合同拒绝，保留失败日志；重新使用允许的 300 秒预算，未改 timeout 合同。
- I 历史完整 smoke 自然运行约 73%，其晚续 source drift 仍不可冒充最终 gate；K handover 独立检查前一终态与冻结输入后才允许运行。未生成最终安装版、未提交完成代码、未核销完整 tasks。

### 2026-10-15 03:20：K portable 真实闭环通过，备份暴露新的合同缺陷

- I 历史完整 smoke 命令自然 exit 0：4528 passed, 23 deselected, 61 warnings in 4473.45s；AgentLoop deterministic/network_calls=0 与 source validation 随后通过。其 wrapper 正确拒绝 freeze 后五个文件漂移，不能作为冻结最终 gate。J handover 正常拒绝，K 在完整输入再次确认无漂移后于 `2026-10-14T19:17:59Z` 接续唯一完整 smoke（runner 61012），未并发启动第二个。
- K portable 实际绑定 3124/8124，API/frontend/BFF/raw/runtime health 全 true，DEGRADED 表示远程未配置，不是联网能力通过。独立完整 UI 成功：正常创建本地账号；真实 PDF 导入与同 bytes 再导入只有一个 canonical/material identity；真实 18 页 PDF 和第 2 页 canvas；笔记真实持久化及刷新；PDF mode/page 2 刷新恢复；个人记忆候选/确认/停用后刷新仍 disabled r3，remote policy false。pageerror 及 browser 外网请求为空。此为 portable production BFF/browser scope，仍非 installed Electron。
- K portable 输入 79972 个资源、1936273519 bytes，完整 manifest/size/hash/未登记文件校验通过。CLI 先前非 bundled interpreter 被正确拒绝，随后实际使用包内 Python -B -s，未放宽 isolation。
- 真实 J 数据备份验收 exit 2：`desktop_inventory_required_owner_missing` / `research_rag_local`，缺少从未启用的 `research/catalog/chunks`。backup 未产生有效 ZIP、未进行 restore/activate；23 个源审计文件 hash/size 前后不变。不得制造空索引目录掩盖问题。正在根因修复 local-only runtime 与 required storage contract 的差异，已存在/启用 RAG 的完整性验证保留；完成后必须新建 L 冻结，不能继续宣称 K 最终通过。
- K native 后续独立 helper 因复用 root 的 STOPPED startup projection 拒绝；实际本次启动还是旧状态瞬态仍在审计，不先删终态条件。原失败 evidence 与正常 owner shutdown 记录保留。
- 独立 candidate packaged directory 首次下载 Electron 网络 timeout，保留失败日志；后续使用已存在且实际验证版本 44.7.1 的 Electron distribution，未改生产配置或跳过 runtime inventory。候选目录不是最终安装构建或安装验收。

### 2026-10-15 03:30：候选打包目录已构建，但完整包验收正确拒绝

使用同版本已验证 Electron distribution 44.7.1 构建独立 packaged directory，electron-builder exit 0；产品 Agora Paper Agent 0.1.0.0，实际 Authenticode=NotSigned，不能把日志 signing with signtool 当作已签名。EXE SHA-256 `b48f3f9361fada8ceb020c366a10e32752cc28942532173dbaf64df90a995068`，app.asar SHA-256 `38da9f8cab167b33bf2e2d6468d34acaac52037c7cee65d2514476a0e1adb3b2`。

主控对真正打包输出重新执行同一完整 runtime validator，正确失败：`resource frontend/node_modules/cssstyle/lib/generated/.gitkeep is missing`。进一步精确 inventory 差集发现 146 个缺失文件、0 新增，包含 hidden/pyc/长路径第三方 license。未启动该包、未修改 stage/manifest、未把 builder exit 0 视为包装完整，更未删除资源或许可来通过检查。正在对生产 packaging copy 和最终验证钩子做根因修复，并保持严格 owner/size/hash/extra-file/reparse gate；需新 L freeze 后复验。当前其他 local-only backup 合同修复也进行中，K smoke 将保留为实际历史检查，不虚称无漂移最终通过。

## 2026-10-15 04:15 集成检查点：包库存根因、原生剩余动作与未完成范围

- 修正早期包差异计数：普通 Windows 路径扫描得到的“146 缺失”含长路径误判；对 namespaced path 完整库存与逐文件 hash 复核，实际缺失为 **22 个 `.gitkeep`**，其余 79,950 个资源全部匹配。根因为 builder 的 glob walker 在 matcher 前忽略这些文件，不是 bundle manifest 错误。
- 生产打包改为 manifest-owned 的独立、排他复制（非硬链接），拒绝已有目标、source/destination 别名重叠、reparse、未列出或被修改的文件，并在复制、签名后与最终 artifact 阶段进行完整 gate；不重写 manifest、不修补旧包。匹配检查为 desktop 18 files / 86 tests 及 typecheck。
- `.tmp/paper-agent-desktop-app-20261014/candidate-l-packaged-dir-local-electron/win-unpacked` 已由实际 builder 构建并通过 79,972/79,972 资源、1,936,273,519 bytes 的完整校验。**该目录名含 l，但 payload 仍是 K，证明范围仅为打包根因修复；不是重新冻结的 L release，也不是安装/签名验收。** Python/host EXE 当前未签名；Node 保留原有有效签名。
- 原生 development host 的剩余交互已实际通过：18 页真实 PDF 的第 2 页和 exact source 恢复、1.25 zoom、Ctrl+K、本机响应式 drawer、专注往返、真实 history/lifecycle 一致；最终 zoom 恢复 1。该运行无 page/console error，精确 owner PID 44272 由正常 drain/STOPPED 退出，3120/8120 已释放。此证据仅补充剩余 unpackaged 动作，不冒充整流/packaged/installed、物理输入法或电脑重启证据。
- native v4 诊断已单独确认 memory disabled revision 3、默认不外发策略及正常 reload 后保持；其整体失败是 helper 在 PDF 保存回执之前读 history。后续 helper 使用真实 PUT ACK 与精确 GET，旧失败证据保留，不调整生产业务状态来让 helper 通过。
- 目前仍欠缺 PRD F-08 / task 7.3 的独立、明确确认的数据清除入口；正在补齐 owner 受控停止、预览身份绑定、有限清除、凭据/会话和失败安全合同。默认 packaged profile 尚未创建，不能先污染用户默认根再用手工删除冒充产品能力。
- K 全量 smoke 仍自然运行，未取消/并跑；之后的 recovery/packaging 修复会使冻结后 wrapper 如实报 drift。即使命令最终 exit 0，也只能作为该运行证据；最终代码仍需重新冻结并通过完整 smoke。联网 provider/来源/费用授权、真实问答/报告导出闭环、失败场景、备份恢复迁移、安装升级/卸载、电脑重启等范围未核销，最终目标尚未完成。


## 2026-10-15 04:34：K gate 终态与后续实施

K 全量 smoke 已自然结束，session 50991 terminal：实际测试命令 exit 0，pytest `4528 passed, 23 deselected, 61 warnings in 4422.01s`；deterministic AgentLoop `network_calls=0`，source validation valid、0 errors、0 warnings。日志 SHA-256 为 `0b17e3cdf561d4d1d5092ad6323638fcc0dc4cb61c80d1adec10e388acb13f68`，主控重算匹配。runner PID 61012，helper PID 67560；未取消或并跑第二个全量 smoke。

冻结后检测为 added 7 / changed 12 / removed 0，HEAD 未变；wrapper 正确返回 failed/2，handover 不继续启动下一 gate。该结果仅证明本轮命令通过，**不是最终源码 gate**。L freeze 尚未捕获、L gate 尚未启动，必须先等 production 修复与安全清除实现完成。

主控已补 desktop-shell 清除 requirement / design D10，strict 校验通过，并由三个互斥 worker 实现 Python owner/finalizer、Electron bridge/session/原生确认、设置 UI。旧 profile 不作实际删除测试；只清除本轮新建的受控 fixture。另有独立 worker 处理实际 125% 截图里的 PDF toolbar 裁切，避免把外层 overflow hidden 当无布局缺陷。完整范围见 `acceptance-gap-audit-20261015.md`，没有把后续工作缩减为安装验证。

Recovery 的 current-source + actual J data 隔离 restore 已返回 VERIFIED，但第三轮发现源 memory SQLite 字节审计变化，仍在定位 WAL/checkpoint/helper 路径；不伪称 23 个源文件始终未变，失败回执原样保留。该证明既不激活恢复，也不操作 K 的 live 实例或账号凭据。

## 2026-10-15 05:14：清除入口前端、PDF 布局与集成审查

- 已提交 docs-only 53ea4938，范围仅本 change 的清除安全 requirement/design 与证据/AC 矩阵；未提交未通过最终 gate 的生产代码，也未推送。
- 正式设置及 local-desktop 登录前已实现同一 clearData 安全入口，缺 bridge 显式不可用，不开放其他账号论文/记忆。成功 preview 必须 CONFIRMATION_REQUIRED；精确中文确认、原生取消、过期/消费和 UNKNOWN/false PREPARED/FAILED_PARTIAL 分开反馈，不把未知结果说成数据未变。匹配测试 5 files / 51 tests、全 feature typecheck 与 scoped lint 通过；Python/Electron owner、并发及退出后 finalizer 仍在集成，尚非完整 clear 验收。
- 主控第一次全前端整合为 1063 passed / 1 failed，发现 palette guard 禁止命中原 PDF document 的新 CSS；保留失败日志，未修改/削弱该断言。根修采用独立 page-flow layout wrapper 承载几何，原 document/ref/canvas/annotation 子树保持不变，默认 Portal display:contents 隔离。PDF 65 + palette 4 单测及真实浏览器 7 场景通过，包括原色与 annotation 坐标。
- 修复后主控全前端复跑 **174 files / 1074 tests 全部通过**（candidate-l-pre-freeze-frontend-tests-r2.log），全 feature typecheck 再次通过。这是 pre-freeze 集成检查，不是最终 release 或原生 Electron 125% 复验；最终候选仍需重新构建与冻结。
- K private portable 已用其真实 bundle Python 经既有 owner 正常 drain/STOPPED，session 29660 exit 0；3120/8120/3124/8124 全部无监听，既有数据及资源保留，未操作用户普通 3000 服务。正常停止与端口 proof 位于 PRIVATE，未用 kill-by-name 收尾。
- 安全审查正在修复 clear 的 maintenance/startup writer 互斥、readonly status 不消费预览、pending/partial 明确重确认、精确 document/network fence、finalizer 父进程/断管与超时、整个 profile 唯一退出后删除者、真实 Chromium profile 顶层 owner 白名单。未完成这些不能启动 default packaged profile 或声称 task 7.3 完成。

### 2026-10-15 05:40：冻结前复核

当前恢复复测业务结果成功，但最终回执因并行 owned_clear.py 输入漂移保持 FINAL_RECHECK_FAILED；不得将其包装为最终稳定源验收。完整前端 1076 项及 Electron 116 项/类型检查/构建已通过，实际清除与最终 L portable/packaged/installed 均仍待验证。清除实现发现并在修复 retry PREVIEW 解锁风险与删除前路径替换风险；必须完成匹配测试后再冻结。K/既存资料与服务保持停止和保留状态，不以手删 profile 取得空库证据。

### 2026-10-15 06:49：L managed 真实闭环通过；实际 NSIS 安装资源缺失，拒绝放行

- 同源 L portable stage `desktop-ec29d6d29f4793d8df475676` 的79979资源 / 1936597095 bytes完整校验通过；manifest SHA256 `2e128a6e66f65b21e6ab4306ee6600c7101bf6075e599e3c0576a2fe2cd96763`。真实 managed UI 使用bundled Python/Node和新私有profile完成普通登录引导、真实18页PDF导入及重复导入复用、page2 canvas与reload保持、笔记真实API回读、记忆确认/停用/reload及外部策略false。实际UI导出JSON落盘并与API完整records identity/revision/status核对；未把测试fixture假称用户导出。证据 `candidate-l-managed-ui-evidence.json`、`genuine-local-memory-export-l.json`（均位于 `.tmp/paper-agent-desktop-app-20261014`）。此后普通停止成功，未删除profile。冷启动约8分钟，保留性能问题，不冒充流畅冷启动。
- L NSIS构建完成，build hooks和主控独立校验的win-unpacked资源全部通过；installer 554604675 bytes，SHA256 `8109922e528216ed5b3f1ceda817fb626e58e58766e208886b2856798501a3bf`。宿主与installer为NotSigned，不算签名发布证明。
- 在确认精确产品安装目录、注册项、两个快捷方式和两个默认profile均不存在后，真实per-user NSIS `/S /D=...`安装完成，installer PID65744 / creation `2026-10-14T22:19:46.0766796Z` / exit0。但实际installed inventory缺9文件（4个ARM PE、5个torch license），不生成通过approval/proof，未启动安装版App。真实exit0不等于安装成功；失败回执 `candidate-l-installed-inventory-failed.json` 永久保留，不补文件、不改manifest、不删授权许可文件。
- 主控从原始同一个installer精准提取9文件到新PRIVATE目录，9项size/SHA256全部与79979资源原manifest一致（`candidate-l-original-installer-nine-resource-proof.json`），证明构建archive并未漏打。真实ARM成员使用ARM64 LZMA2；5个license在实际安装路径长度为262/267/281/281/278，系统LongPathsEnabled为0，未改设置。
- 独立真实NSIS小fixture分离复现根因：默认ARM64过滤器时，old Nsis7z返回流程exit0、普通control存在，但4个ARM全缺；相同真实文件显式无filter压缩后4个全部size/hash通过。无ARM的long-license-only fixture仍丢5文件，CopyFiles错误exit17，普通control正常。证据 `candidate-l-old-nsis-decoder-rootcause-proof-r5.json`；早先混合fixture/脚本自身路径及语法失败保留，不作为成功证明。接下来验证现代embedded decoder与长路径安全解压，不仅修改ARM过滤器，也不借修改系统策略绕过。
- 为避免留下已知不完整安装，严格验证安装回执/EXE hash/注册项/快捷方式归属、所有祖先无reparse、无运行宿主、默认profile仍absent后，使用该安装自带的真实uninstaller `/currentuser /S`，未传delete-app-data。PID66944 / creation `2026-10-14T22:47:29.7441098Z` / exit0；13.57秒后精确安装目录、注册项、两个快捷方式均已不存在，两个默认profile仍absent。没有手工递归删除、补文件、运行安装版App或清除任何K/J/native/既存profile。证据 `candidate-l-failed-install-owned-uninstall.json`。
- L4435源冻结仍完整无漂移；唯一完整smoke尚在运行，不能声称通过。生产代码尚未因NSIS根因修改；PRIVATE修复prototype与失败证据不改变L source freeze。安装版完整UI/真实清除、独立非开发环境/物理输入法/重启、授权后provider检索与Harness闭环等仍未通过，完整AC与tasks保持待验收。

### 2026-10-15 07:01：原始 L installer archive 独立完整性补证

现代7za对原始同一installer的完整CRC测试exit0 / Everything is Ok：80055 files、9099 folders；NSIS exe overlay引发的data-after-end warning原样保留，不当作无warning。独立逐项比对79979个runtime成员的路径/尺寸与原manifest完全一致，无duplicate、无extra runtime member；此项不等同于逐成员SHA验证或旧NSIS安装成功。9个实际缺失成员另已精准提取fullSHA通过。证据 candidate-l-original-installer-full-modern-test.log 与 candidate-l-original-installer-archive-members-proof.json，均在 .tmp/paper-agent-desktop-app-20261014。


### 2026-10-15 10:30：L 完整 smoke 终态已独立核验；进入 M 根因修复

L 唯一完整 `python -m scripts.dev smoke` 于2026-10-15 07:10:08（Asia/Shanghai）实际终态exit0：4528 passed / 23 deselected / 61 warnings / 4448.99秒；compile、deterministic agent-loop（network_calls=0）与source validate也完成。主控10:24重新核验原始log SHA256 `7ced8d940b4475bf94b2426a1055428c0313338e44557136af6fcf6d8b57e9d3`与terminal receipt一致，原runner/helper已不存在；完整4435输入冻结仍无HEAD/added/removed/changed漂移。证据 candidate-l-final-smoke-receipt.json 与 candidate-l-final-smoke.log，旧失败及L安装缺9文件不改写。

这仅证明L冻结源的完整smoke，不代表NSIS实际安装/清除或远程闭环合格。下一版本M需修复已证实的installer ARM过滤器/长路径问题，以及自包含提交快照暴露的真实JSON原子写入长路径失败。后者由长目录下的真实PDF导入触发，run intent临时文件超过260字符导致run_intent_persist_failed；不通过缩短testroot、减少UUID或修改系统LongPathsEnabled掩盖。初始自包含候选定向检查178 passed / 1 skipped / 2 failed / 18 errors全部保留，诊断明确18项setup失败来自该长路径业务写入；另2项揭示legacy catalog artifact portability的未纳入基础依赖。证据 owned-cleanroom-desktop-recovery-tests-initial.log、owned-cleanroom-import-diagnostic-r2.json。此快照尚待五个own-only混合文件与必要基础依赖审查，不是最终提交快照或发布验收。

M生产修改授权后，L冻结与L通过回执保持不可变；不以L验证修改后的源码，不并发重复fullsmoke。实际安装版尚未启动，真实provider/论文源/费用授权仍未取得，所有相关完整AC保持未核销。

## 2026-10-15 12:12（主控：M 自包含依赖与长路径分层验收）

- 本轮明确使用 `paper-agent-desktop-app`，保持 paper-only 正式 `/paper-agent` scope、离线/费用授权边界，以及原全部质量门；不以 L 成功代替 M 源码验收。D11 与新增 Windows lifecycle requirement 经 `openspec validate paper-agent-desktop-app --strict` 校验通过。
- PRIVATE 自包含 candidate 位于 `C:\Users\Administrator\AppData\Local\Temp\pa-paper-agent-owned-source-k6nfynmc`：从实际 HEAD 加经审查 own hunk 与显式必要 local-desktop foundation 组装。为实际 Git history/tracked-source architecture proof 增加独立 PRIVATE Git 元数据，读取真实 object history，不伪造 commit/删除证明；生产 index/HEAD 未变。原 300 passed / 4 failed 记录保留；补齐真实 Git 元数据后 architecture/factory/memory/composition 308 passed，剩余 dynamic production E2E 失败未隐瞒或跳过。
- 自包含 frontend 全检查发现 `.gitignore` 的 `lib/` 递归漏跟踪既有 frontend source：初次 157 suites passed / 10 failed、958 tests passed，typecheck 缺模块，不能把普通工作树可运行当成从提交可构建。显式按 HEAD 已有真实 imports 补齐 15 个既有 runtime/test assets；窄规则只解除 `frontend/src/features/news/lib/` 与 `frontend/src/features/studio/**/lib/` 忽略，Python/generated/runtime 继续忽略。没有创造补位 module、删测试、排除失败 suites、引入新的其他业务功能或纳入无关 Lab baseline。
- 正确 PRIVATE cwd、fresh offline npm ci（不借用 workspace node_modules）后，完整 frontend **169 test files / 1,012 tests passed**，lint / typecheck / local-desktop production build 均 exit 0，正式 `/paper-agent` 存在。前端源码 869 inputs 与日志 SHA 绑定于 `F:\github\NewsRoom\.tmp\paper-agent-desktop-app-20261014\owned-cleanroom-frontend-success-receipt.json`。窄 Git source-tracking contract 3 passed。仅证明该 frontend candidate，不冒认 installed UI/provider/reboot/full M smoke。
- Guided enabled marker 缺 owned model path 的错误分类根因已修：marker 与非空专属 `NEWS_RESEARCH_MODELS_CONFIG` 先于共享 defaults/凭据解析准入，共享 config 不可替代。真实 cleanroom 红阶段 12 failed / 15 passed，修复后 repo 与正确 cleanroom 各 **27 passed**；原 disabled/unmarked/owned route 与原断言保持，test socket 禁止外联。证据 `candidate-m-guided-marker-receipt.json`，不代表 provider 实际业务闭环。
- 一行 configured RAG caller `transcript_root=settings.rag.transcript_root` 作为显式必要 HEAD 接口 foundation 审核纳入 PRIVATE proposal，不假称 initial own；没有引入 factory recorder/evaluation baseline。真实 matching checks 64 passed / 1 failed，失败明确来自 interfaces JSON journal 完整 SHA/PID/thread/UUID temp 的普通 Windows 长路径。正在修复中立 shared native path I/O、保持 public logical path、完整原子锁/UUID/fsync/retry 与 cleanup 语义；不缩临时文件名或降低目录长度。
- JSON run-intent 长路径第一层修复已使原真实 PDF fixtures 正常通过 setup；backup/restore 下一层 snapshot copy 长路径失败记录保留。完整 recovery namespace I/O 与 fixture 精确 owned 长树 cleanup 后，正确 cleanroom **77 passed**（local-only recovery integration + existing recovery + explicit long-path cases，真实 PDF/source bytes/WAL/canonical identity/notes/memory/session rules保持）。此为范围测试，不是新 M installed 或 actual J recheck，后续整合仍需完整验收。
- Dynamic production E2E 的 `BUDGET_EXCEEDED` 已确认是 failed 子任务后有界 retry 耗尽的终态，不是获准加预算/跳 gate 的理由。保留原原始失败及只读 observer 证据，继续追踪实际 child execution cause；尚不宣称该 pipeline 通过。
- 安装资源链仍在生产级修复：不补安装缺失文件，不缩 manifest，不修改 Windows LongPathsEnabled/Defender，不 fork/patch官方NSIS模板。原 L 安装缺 9 files 与精确安全卸载记录保持。M 需完成 pinned modern extractor、目标准入/全包独立 hash 验证、长树默认卸载/升级保留数据、真实完整安装与实际 App clear 验收后才能交付。
- 生产代码仍未 commit；M 输入尚未冻结，不启动与并行生产修改互相漂移的 full smoke。完成 scoped root fixes、正确 candidate 检查与新冻结后，必须执行真实 `python -m scripts.dev smoke`、OpenSpec strict，再仅提交 own/显式必要 foundation。任务 6.1 以外未新增勾选，goal 保持 active；未获来源/provider/费用授权，不发线上业务请求，独立非开发机/签名/物理 IME/电脑重启等仍未冒认完成。



### L freeze coverage 的追加审计（不重写历史回执）

- L 的真实 4,528-pass terminal、log SHA、其定义的 Git-tracked/non-ignored freeze 检查结果均保持原样。但本轮自包含审计发现既有 frontend `lib/` 源码被 Git ignore 漏掉：这些文件可以进入源码构建，却不在旧 freeze 的 Git 枚举集合中。不能把“集合内无漂移”升级成“所有真实 build inputs 都已覆盖”；不会回填或改写 L freeze 来伪造历史保障。
- 新 M 必须使用经审查自包含提交候选，并完整覆盖实际源码/构建输入、显式必要既有 ignored source assets、依赖 lock 与 packaging/NSIS模板版本/摘要；排除生成物/用户数据但不能排除真实代码。窄 `.gitignore` 修复使上述 15 个必需 frontend assets 可正常跟踪，并已有真实 fresh Git fixture 与自包含 production build 检查。L 不替新 M release gate 背书。

## 2026-10-15 12:56（主控：M 存储根因修复已定向验证，独立提交门进行中）

- Harness dynamic production E2E 已由只读真实异常 trace 定位：`LocalJsonConversationStore.append_message` 长目录 `mkdir` 的 WinError 206 → durable child evidence retained → trusted child halted → bounded retry 后 budget exhausted。保留全部原始 terminal / observer 文件，没有加预算、放松 gate、缩 ID/目录或改 routing。中立 `framework.shared.windows_paths` 的 native I/O 修复覆盖整个 conversation 生命周期后，原 dynamic production E2E **1 passed in 94.35s**；完整原 conversation suite＋新真实长路径生命周期 **18 passed**。该 fake worker/source/compiler/RAG 测试证明真实 harness path，不等价 provider 实测。
- interface JSON journal 的完整 PID/thread/full UUID temp 269 字符失败已根因修复。两个 JSON owners 共用中立 shared helper，不产生 interface → infrastructure 依赖、不保留旧 private compatibility helper；完整 UUID、exclusive temporary open、flush/fsync、8 次 PermissionError retry、仅清理本次创建 temp、主异常不被 cleanup 掩盖。repo 与正确 PRIVATE candidate 均 **59 passed / 0 skip / 0 network attempts**（infra JSON 29、interface JSON 25、paper transcript 4、composition 精确 transcript 1）。回执 `candidate-m-json-shared-path-receipt.json`，所有实际中间失败原样保留。
- 已补齐此前明确缺的组合验收：真实 personal-memory **source 359 字符 / restore root 345 字符**，active / stopped-with-shm / stopped-without-shm 三种 WAL 状态各走 `BackupManager → archive → RestoreManager → application validation → logical public-store readback`，不以裸 SQLite helper 代替 manager 链。
- 上述组合测试真实驱动了 personal-memory adapter 的 long-path parent/file/sidecar/access/connect 修复；公开 database_path 维持 logical，规范化前拒绝 linked/reparse 输入。业务 record revisions `[2,2,3]`、policy revision 3、remote false、未 checkpoint WAL 内容、source main/WAL/SHM bytes/存在性/签名不变、未确认 activation 拒绝、独立 shadow owner activation 后 generation 仅旋转一次均经断言验证。完整 recovery 原 77＋新增 3 = **80 passed / 0 skip**；完整 adapter tests repo 与 PRIVATE candidate 各 **15 passed / 1 skipped**，唯一 skip 是既有 file-symlink creation 的 WinError 1314 权限限制，真实 junction case 保持 passed，未新增 skip。
- 对已完成且独立的存储修复，主控新建 `C:\Users\Administrator\AppData\Local\Temp\pa-storage-rootfix-r65m__ky`，仅真实 HEAD＋9 个 shared/JSON/conversation/personal-memory 生产及 matching tests 文件；不带其他 dirty desktop/frontend/RAG/recovery foundation。该独立提交候选 **91 passed / 1 existing permission skip**，完整输入摘要已固定，必需 `python -m scripts.dev smoke` 正在实际运行，尚无 terminal 结果，不得预报通过。记录 `storage-rootfix-commit-candidate-preparation.json`、`storage-rootfix-commit-candidate-smoke-receipt.json`。
- M 全范围 PRIVATE 后端集成另在原正确候选 cwd 运行；安装器独立 worker 仍做官方模板/完整真实包验证，不与存储独立候选混报。官方 stock installerSection Function 定义位置问题已用真实 positive/negative compile 检查；尚须交回完整最新实现/单位测试/真实长树 lifecycle/安装全包记录。未启动默认 App、未读取真实凭据、未发 provider 请求，生产 HEAD/index 到本节记录时仍未变，goal 保持 active。

## 2026-10-15 13:25（主控：自包含定向检查终态与新增导入阻塞）

- 正确 PRIVATE candidate cwd 的后端根因修复集成已取得真实 terminal exit 0：**511 passed, 1 skipped, 97 warnings in 512.95s**。范围含完整 architecture、JSON owners、conversation/Harness dynamic fake-worker 真实运行链、personal-memory adapter/context、composition、recovery 和 source tracking。唯一 skip 仍为既有 Windows file-symlink 创建权限限制，不是新增长路径或 WAL 用例的跳过；本次不是最终 M full smoke，也不是 provider/安装版 E2E。证据 `owned-cleanroom-m-backend-rootfix-integration-receipt.json` 与同名 `.log`，日志 SHA256 `44f5e0078fab14d17ced6447d75b8a0699a117283615b62fb6989506439da38e`。
- 前端所有显式必要 source foundation 后，169 个测试文件 / **1012 passed**、默认 typecheck、正式 local-desktop production build 均 terminal 0；新增 15 个源文件之后又单独重跑完整 src lint，terminal 0，补充回执 `owned-cleanroom-frontend-lint-final-supplement.json`。历史 L 的 ignored-source 覆盖缺陷没有通过改写旧 receipt 掩盖。
- 后续新增 auth/import/memory/application 集成真实结果为 **143 passed / 2 failed**，原日志 `owned-cleanroom-m-auth-import-memory-application-integration.log` 保留，SHA256 `a7fab29fec0cf77c735f9856a05550ecdb1e60fa9c90f8e251ad067297c63a55`。两处失败为真实认证桌面导入与 owner-scoped PDF content identity 的 HTTP 500 / `FileNotFoundError`，尚待根因诊断；不能将之前 511 passed 外推为这些用例通过。另有 Qdrant compatibility warning，须查构造路径并使新验证禁止无授权外发。没有削弱响应/去重/重开断言。
- 独立 HEAD＋9 文件存储提交候选的必需完整 smoke 仍运行，实际 pytest 子进程 CPU 在增长，尚无终态；未重复启动、未中断、未缩检查集合、未 stage/commit。安装器完整清单容量检查由继任 worker 继续；原 worker 的 compaction 运行时失败如实保留，不把中间源码或 mini lifecycle 当正式安装验收。



## 2026-10-15 14:00（主控：M 导入复验与最新 installer sidecar）

- 原失败 API 集合的同 11 个测试文件已在 PRIVATE candidate 正确 cwd、Python 3.12.7、无凭据/外网 guard 下复跑：**154 passed / 67 warnings / 38.56 秒**。原集合为 145 cases，新增长 import-root 与临时文件生命周期 9 cases 后为 154，不将数量差异隐去；原 143 passed / 2 failed 日志保留。最终 PDF 写入独占创建临时文件，只清理本次创建文件；fsync 后 replace，清理失败作为 note 保留原异常，碰撞不删不覆写。原始复验日志 `candidate-m-api-import-rootfix-exact-original-suite.log` SHA256 `c71f10f4632c58923ccbcda48949a8aeb8b40e341402c3df3eed7b0bac36d917`。
- 本次原集合 guard 的外网 DNS/connect 尝试为 0、凭据文件访问为 0；有 53 次允许的 loopback connect，不称全部 network_calls=0。通用 local-desktop 未显式禁用 LLM 的 4 个 API factory tests 仍真实构造 Qdrant 并产生 server-version warnings；未改默认配置或 mock 掩盖。单独显式 disabled/local-only、未 mock production preload 的回归通过且 Qdrant construction 为 0。此前扩大 resolver 集合有 4 次被阻断的外网 DNS，仍保留失败/警告记录，不移植到最终原集合。
- 最新 installer 源码已在新的独立 owned sidecar 上通过 **stock electron-builder 26.15.3 官方模板真实 compile exit 0**，未复制/fork 官方模板，产物 181114350 bytes，SHA256 `33afb67465d8c330efb3a0edaaa2b291f8e3611655640e4fca2f58c10541d89c`；此 stock installer 未执行。同时最新 mini install -> upgrade -> uninstall 均 exit 0，4 个真实 ARM64 PE、5 条 314–333 字符 license 路径完整，升级后 payload 为 private-v2，最终 owned install root 删除、PROFILE-SENTINEL 保留。9 个输入与固定工具前后 SHA 稳定，bootstrap SHA256 `6fe7977f6988dfa1eb866d37fcf4683ce74a324a7a08bde2ee7b48a16561f27e`。`stock-latest-sidecar-20261015-135424875/final-receipt.json` SHA256 `14e5f40a13db723902038f28a170a1e87bee2dcad1f904b7f5c7339dc51796df`。这不是正式全量安装、真实 App 升级或清除验收。
- PRIVATE candidate 最新 installer overlay 后，桌面完整 **24 test files / 158 tests**、typecheck/build 均通过，`owned-cleanroom-m-desktop-final-r2-checks-receipt.json` SHA256 `1a58c4e6adebe8121e6f0e4531c4ee438bae1d69c9d2f91275d8cfef3f84ed54`。较早命令在 desktop cwd 使用相对 .venv 路径失败、后续意外跑旧 snapshot 116 tests 的日志仍保留，不能当作最新结果。候选构建解释器是独立复制并逐文件 hash/nlink 验证的真实 3.12.7 dev venv；Electron 44.7.1 来自本地缓存并匹配锁定包 checksum，不借用开发 node_modules。此为构建输入准备，不是 portable 运行依赖验收。
- 全 data root >260 时 personal-memory composition 初始化仍有普通 Win32 mkdir/chain I/O 根因，已单独分派保留 lexical junction 拒绝的修复；上述长 import-root 通过不等于任意长 data root 通过。9-file storage 独立完整 smoke 仍运行，未提交、未复用 L 门、未启动重复 full smoke。所有原 tasks/完整 AC 状态保持，M 新 release/最终完整安装/installed UI 清除/provider 授权等仍未完成。


## 2026-10-15 14:28 独立完整 smoke 终态与最终整合候选冻结准备

- 原 HEAD + 9 个 Windows 路径/存储修复文件的独立完整 `python -m scripts.dev smoke` 已于 14:21 结束：**2 failed, 4202 passed, 23 deselected**，5191.28 秒，全部 4202 个 source 输入无漂移。原失败日志及 receipt 原样保留，不计为通过。两个失败均为 HEAD 已有 personal-memory adapter 的实际 domain/port import 未登记到 architecture 的两份精确 contract；整合候选仅登记该 adapter 的两个真实依赖，不放宽扫描或断言，也不混入其他评测例外。独立十文件候选正复验同一修复边界。
- 个人记忆 composition 的超长 root 初始化根因已修复，保留 shared research settings 的完整校验合同和 lexical junction 检查；生产工作树与私有候选各 **18 passed, 0 skipped**，包括真实超长目录 reopen 和祖先 junction 拒绝。证据位于 `personal-memory-composition-long-rootfix-20261015-141226-379251/manifest.json`；原 154 项 API 集合早于此 composition 最终版，不把它当作新版 composition 的统一验证。
- 接下来冻结 actual HEAD `0070821de66d53ff0d5cbdd6a619c4975bbc6e69` 加审阅后的本变更/必要基础输入，运行唯一新版整合候选完整 smoke，并与真实 source release / portable runtime / stock NSIS 构建并行。该 HEAD 只标识并发文档提交后的基线，不冒称本变更代码已经提交。生产 index 仍不得混入其他变更；完整 gate 通过前不提交生产代码。
- 此记录不宣布正式安装、完整 runtime 文件安装后清单、实际 App 数据清除、外部 provider/来源/费用授权、签名或独立机器验收完成。


## 2026-10-15 15:00 扩大检查根因修复与 M-r2 重新冻结

- 首个整合候选在原 11-file API/记忆集合实际 **167 passed, 67 warnings, 17.59s**；external attempts=0、env-file read attempts=0、loopback connects=53。该次关闭 Python trace，因此旧 runner 的 `QDRANT_CONSTRUCTION_COUNT 0` 是未启用计数器，不是无 Qdrant 构造证明；原 tracing-on 4 次 generic 构造、显式 disabled 0 次仍只在其独立范围成立。
- 在未缩小选择的全部 `tests/scripts` + storage + memory-adapter 范围发现 **3 failed, 1301 passed, 95 skipped**。95 项是既有外部 PostgreSQL/Redis 显式 gate 或本机 symlink 权限条件，不能当作全部执行；两个失败为 HEAD 的三份旧 PDF parser 用 `import fitz` 在 import 阶段打印弃用提示，污染 JSON CLI `doctor`/`inventory`，另一失败为 HEAD 已有测试依赖的 pending compatibility-policy JSON 被全局 `openspec/` ignore 漏收。
- 同根因也导致新 portable Python probe 严格 JSON 解析失败；预修复 source release `20261015T063716Z-e6e66de81290` 构建成功不等于 portable 或安装通过。原 source build、probe 失败、临时 bundle、日志与终态 receipts 全保留，不修补或复用旧 payload。正式 NSIS 尚未启动。
- 对已经确认范围失败的首个 M 完整 gate，主控核对精确 owned pytest PID、出生时间、可执行文件、全命令与 parent chain，确认无子进程后停止；终态 exit=4294967295、source drift=0，明确是 **中断、不通过**，不是通过证明。旧 4395-file checkpoint 另有 9 份并发 HEAD-only 文档补充 inventory；均不属于 runtime copy roots，2590 真实 build inputs 无遗漏。原 freeze 不重写。
- M-r2 只补入明确必要的旧基础资产：三份 parser 的纯 canonical `pymupdf` import/symbol 迁移（等于 production 已存在的对应迁移，没有其他 mixed baseline 逻辑），以及现有 pending policy JSON 的原始字节（checksum 不变、authority pending、未伪造 activation/qualification）。不设置 `PYMUPDF_MESSAGE`、不过滤 stdout、不弱化 JSON parse/assertion、不跳过失败。该 policy 仅作为既有测试依赖入候选，不核销 durable-event change 的验收。
- 独立十文件存储候选已验证所有 architecture **234 passed**；存储范围 **91 passed, 1 existing symlink permission skip**；原 HEAD 两个架构契约失败可复现，修复仅两个 exact mappings，共 8 行，不放宽扫描/断言。仍不拿这些定向结果替代完整 smoke。
- 新 M-r2 freeze 使用 HEAD-tracked + private-index + nonignored untracked 的完整并集，确保并发文档也直接进入 checkpoint；随后重新运行原封不动的必需完整 smoke、同一全部 storage/scripts 集合和真实新版 source/portable/stock-NSIS 构建。所有最终安装、UI、原生数据清除与 provider 授权事项仍待实际证据，不提前勾选 tasks。


## 2026-10-15 16:40 完整 M-r2 gate 终态、真实安装与 artifacts 长路径根因

- M-r2 完整 `python -m scripts.dev smoke` 的 pytest 阶段实际 **4238 passed, 23 deselected, 61 warnings, 4485.67s**。随后原命令 `interfaces.cli.news dev run-test-agent-loop --topic "AI agents" --artifact-root .newsroom/smoke --json` 返回 `REF_SNAPSHOT_WRITE_FAILED`；完整 smoke **exit 1，不通过**，source drift=0；原 receipt/log/freeze 均保留。不能用 4238 项通过冒充整条 gate，通过前不得提交或生成 UI 许可。最后 source validate 没有在该失败流程中执行；另行独立运行原 source validate 得到 `is_valid=true/error_count=0/warning_count=0`，只算独立前置诊断。
- 主控原路径重现已追到 `framework/agent/artifacts/stores/fs_safety.py::verified_atomic_write` 的 ordinary-path `os.replace`：64 位 snapshot 文件名把目标推过 Windows MAX_PATH，报 WinError 3。正在正确 I/O 边界调用 neutral `framework.shared.windows_paths`，保留 atomic-write/inode/parent/lock/reparse/immutable/index 合同，不改 smoke root、不缩短候选目录、不削弱或跳过 gate。新 ordinary >260 字符 roundtrip/index/atomic/junction 回归与全既有 artifact/ref-store 测试一起验证。
- 同一 M 独立 `.venv/Scripts/python.exe` 3.12.7、相同 cwd 和 `.newsroom/smoke` 原 CLI 已独立复验成功，`run_id=test-agent-loop-703c1784ccba44ef9b05ff0dd1ba928d`、`status=succeeded`、`network_calls=0`；只证明根因修复后的这个 phase，不等于新版完整 smoke。worker 未限定 `python` 首次误落 C:\Python314 缺 cryptography 的日志无效、原样保留；未靠换解释器、设置输出过滤或更改 artifact root核销。
- M-r2 正式构建事实：source release `20261015T070504Z-86e66e63e2a6`，source digest `86e66e63e2a66257077362e0706ad617bfcefbecd3498aec6386556932ae46a2`；portable bundle `desktop-de44e85221c4c3650500111f`，79920 resources，manifest `030e5b774224d0e7760e6b11cb75d3860a4e0006cdd25a4acc910cb7ee1f869d`；NSIS 624160518 bytes、SHA `80d1b4a2aa3ea4ff37d29e5edda547164bf1f830f30fb59cf0ae7070ae42fda8`、NotSigned。07 首次最小 PATH 漏 PowerShell 构建失败保留；08 补系统 PATH 真实成功。构建中曾新生四个 literal `%SystemDrive%` cache DB，归档完整原始产物后 final source checkpoint恢复完全一致；准确口径是「最终 0 漂移」，不是「全过程从未出现 untracked 增量」。
- 已在当前开发机执行唯一一次正式 `/currentuser /S` NSIS 安装，PID59232/birth `2026-10-15T07:35:09.4566915Z`/exit0。真实 installed root 的 **79920 个 resources** 全 SHA/size/exact set/no extras/reparse/hardlink 验证成功；之前缺失的 9 个 ARM/深层 license 文件全部与原 manifest 一致，最长验证路径281；整个 sealed installation 的真实 `admit-owned` 也通过。registry、桌面与开始菜单快捷方式均指向该安装根。未启动 App、未清除、未卸载、未补拷缺件、未手工修复。两个默认 APPDATA profiles 仍不存在，J/K 原历史 profiles 保留不动。此真实安装证明只属于 M-r2，不冒称 upcoming rootfix 新版 payload 已安装或独立机器/重启/签名验收通过。
- M-r2 其他冻结结果：storage/scripts1304通过95既有条件跳过，API/记忆167通过，composition/catalog/task-context93通过，frontend169文件1012通过且lint/typecheck通过，desktop24文件158通过且typecheck通过。新源变更之后这些保留其原 hash 范围，不篡改历史 receipt。
- 生产 index 仍为空，无任何代码提交。commit guardian 和 installed-UI helper 仅 W 准备，禁止把失败 fullgate转换为 pass/生成占位 approval。待 artifacts 根因实现与真实定向回归完成后建立 M-r3 新不可变 freeze，再重新跑完整必需 gate、范围集成及新版 source/portable/NSIS；实际 UI、原生取消/清除/finalizer/freshbootstrap、provider许可与其他验收仍未完成。
