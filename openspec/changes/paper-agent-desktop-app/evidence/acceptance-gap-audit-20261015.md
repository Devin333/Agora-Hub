# AC-01–AC-16 集成验收矩阵（2026-10-15，未发布）

依据 `prd.md` 第 11 章与当前实际回执整理。下表的“局部证据”不是验收核销；所有 AC 仍待同一最终 source/release/config/dependency 的证据闭合。任务仍为 1/35。不得把未见到的结果写成 PASS。

| AC | 已有局部证据及边界 | 仍需完成的实际动作 |
| --- | --- | --- |
| AC-01 | development Electron 正常启动、真实 owner drain、portable K API/frontend/BFF readiness 全真；manifest-owned builder dir 完整资源校验。不是安装入口证据。 | 最终源码重建 portable/完整包；真实 NSIS 安装与桌面入口、重复启动/端口冲突、非开发约定环境验证；签名状态如实记录。 |
| AC-02 | K 真实论文页面 light/dark 截图与 sidebar 品牌检查；无 A 图标/副标题。 | 在最终实际 packaged/installed 候选复核截图及主要交互；不得用 lab 或旧截图替代。 |
| AC-03 | K portable 正常 bootstrap、真实 owner 空库，API/BFF/private source 鉴权回归；无 cookie 注入。 | 最终候选首次空库与另一 owner 拒绝访问、缺配置/故障状态复核。默认 packaged profile 仍未建立，先完成正常产品清除和收尾合同。 |
| AC-04 | 本地筛选与未配置远程能力的明确降级已实现/测试；尚无获授权的真实在线检索闭环。 | 用户确认检索来源/provider/预算后记录真实请求、来源、零结果/超时/部分失败；本地筛选网络断言。 |
| AC-05 | K 真实官方 PDF 导入、同内容重复导入复用 canonical paper、资料库一行，重开回读；actual source SHA 与 18 页材料。 | 最终候选真实重复保存、收藏/取消、已读、标签/分组、排序、下载/解析状态和失败分支；断网后原件仍可开。 |
| AC-06 | 真实全文、sourceRevision-bound 页 2/18 canvas/locator、invalid page 拒绝与阅读位置恢复。 | 在最终候选分别核验全文/摘要/部分解析三类范围，旧 revision locator 失效和真实定位；不能用全文一个样本代替三种状态。 |
| AC-07 | admission/worker/gate 接线与引用范围单测；缺模型时正确拒绝远程执行。 | 用户授权合格模型后真实 ask/analyze、request/run/transcript/quality 与引用点击定位；无证据提问不得编造来源。缺 provider 不能据此核销。 |
| AC-08 | K 真实 UI 笔记保存、exact BFF CAS 与重开回读；导出/失败合同有局部测试。 | 最终 App 真实笔记编辑/冲突保留；合格报告保存与 Markdown/结构化导出内容核验、真实写入失败不清草稿。报告必须来自真实通过 gate 的业务 run。 |
| AC-09 | local-memory domain/application/SQLite 基础已提交；K normal UI candidate→confirm→disabled revision 3，默认远程策略 false。 | 最终候选手动保存、候选编辑/拒绝、普通笔记不自动写记忆、未确认不可召回、确认后真实重开及索引回读。 |
| AC-10 | 状态/revision/tombstone/索引合同与生命周期单测；disabled r3 正常重开未复活。 | 最终 App 编辑/停用/删除/导出与导出文件内容，后续真实任务不使用 disabled/deleted；重建索引/重启/磁盘失败。 |
| AC-11 | 默认 policy false、按次记忆上下文/外发边界与 unit contract；未进行真实合格模型任务。 | 用户授权后展示真实 memory ID/revision、当前指令覆盖与本次不用记忆；拒绝外发时覆盖模型、tool、embedding 的实际网络断言。 |
| AC-12 | portable/local-only LLM disabled 正常阅读/编辑；不等同于物理断网。 | 实际受控断网下原件/笔记/历史答案/记忆 CRUD，未缓存和远程不可用明确区分；恢复网络不得自动补发。不要擅自全局关闭用户网络。 |
| AC-13 | 真实 owner 正常 drain 和请求身份合同、timeout/unknown/retry 单测；native 多轮正常重开。 | 最终候选受控超时/断连/未知结果核对，不重复付费；故障重开和 pending operation 证据。App 重开不是电脑重启。 |
| AC-14 | J 初次真实 backup 缺 local-only chunks 被正确拒绝；新 recovery 根修与 URI/opaque revision 回归正在复核。current-source + J real data 的隔离 restore 已返回 VERIFIED，但第三轮源 DB 字节审计发生变化，尚未通过完整“原资料未变”证明。 | 定位 snapshot/helper 的 WAL/checkpoint 来源，保留失败记录；重新形成稳定 source audit、manifest/checksum、PDF/笔记/disabled memory/policy/session 失效与代际证明。还需最终候选恢复旧记忆的显式确认、迁移/升级保留/不兼容回退拒绝及独立清除。 |
| AC-15 | native K 实际 zoom 1.25、Ctrl+K、focus 往返通过，限定相应动作；page/console errors 0。主控查看截图另发现 PDF chrome 右侧控件裁切，已分派源码根修，不将 documentWidth==viewport 当无子布局溢出。 | 最终实际候选多宽度/长标题/双主题 PDF toolbar 必要控件可达；125%截图复核、drawer真实焦点与物理中文输入法。IME 尚无实测，不能用合成 composition event 冒充。 |
| AC-16 | 窄 bridge、raw BFF/auth/source/API boundary/architecture tests；strict spec 当前通过。I full smoke command 4528 passed 但冻结漂移，K full smoke command exit 0（4528 passed），但 added 7 / changed 12 的冻结漂移使 wrapper failed/2。 | 待所有代码收敛后新冻结，完整 frontend/desktop/typecheck/build 与必需 smoke 绑定同一最终候选；只提交本变更范围。未完成 gate 不生成最终通过声明。 |

## 当前并行写入归属

- Python clear owner/CLI/finalizer：`scripts/desktop_app` 与对应测试；不触及 recovery owner。
- Electron clear bridge、bounded stdin、native confirmation、session/exit：`desktop/src` 与对应测试。
- 设置 clear 入口：`frontend/src/features/paper-agent-app/paper-agent-settings*` 与独立 clear UI/test。
- PDF 窄列响应式：shared PDF viewer、其 tests 与 `paper-agent-app.module.css`，不碰设置。
- Recovery：`scripts/local_desktop/recovery.py` / `storage_contract.py` / local-only integration test。

## 外部授权或人工环境事项

1. 已用中文向用户询问真实模型 provider、论文检索来源与费用上限；未获确认前不调用付费模型、不自动使用工作区已有密钥。
2. 物理输入法与电脑重启要分别记录；未经用户要求不执行 OS 重启。未具备相应环境的 installed/非开发验证不得拿 development host 替代。
3. 新 clear 合同只是实现要求，不授权清理现有用户 profile；实际删除测试仅可作用于本轮明确新建并持有的隔离 fixture。默认 packaged profile 收尾必须通过同一已验证生产清除入口，不能 shell 删除或读取凭据解锁。
