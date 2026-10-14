## Why

用户已确认以「Linear 的桌面框架 + Bear 的文字排版 + Paperlib / Zotero 的论文结构」构建论文专用 personal agent。现有 `/paper-agent-lab` 只是隔离的界面预览；下一步需要把已选定的阅读体验变成可安装、能连接真实 Research 能力、个人资料与长期记忆保存在用户电脑上的桌面 App。

本次先提交 PRD 与 OpenSpec 实施合同供评审，不生成安装包、不修改运行代码、不宣称后端或真实模型已完成验收。产品需求以 `prd.md` 为主，当前实施状态为未开始。

## What Changes

- 定义 Windows x64 首版桌面 App 的交付边界：独立窗口、桌面入口、可追溯构建与本机运行生命周期，而不是给预览网址加一个快捷方式。
- 将已选定的黑白三栏阅读设计接入真实论文检索、导入、资料库、阅读、笔记、分析与带引用的问答；不把固定样本文献或页面内存当成正式业务实现。
- 按用户最新截图删除侧栏品牌区的 A 图标和 `Papers / 论文工作台` 副标题，保留 `Agora` 名称；此修订是明确的实现与视觉验收要求。
- 定义本地个人长期记忆的保存、查看、修改、停止使用、删除和有来源的召回；区分本地保存与用户主动调用远程搜索/模型时的数据传输。
- 复用 `research-local-desktop-readiness` 中的本机运行、身份、真实操作、报告与恢复合同；新增桌面容器和论文专用产品入口，不重复建立第二套业务存储或运行控制器。
- 第一版严格不包含新闻、项目、邮件、日历、购物、社交、电脑操作、跨设备同步或不受用户发起控制的后台行动。

## Capabilities

### New Capabilities

- `paper-agent-desktop-shell`: 桌面窗口、安装交付、首次启动、本机能力状态与既有运行管理的集成。
- `paper-agent-research-workspace`: 论文专用阅读优先界面、品牌精简、真实文献工作流、来源与失败反馈、黑白主题和桌面可访问性。
- `paper-agent-local-memory`: 本地个人记忆的用户控制、来源、持久化、召回边界、远程数据披露及导出/恢复。

### Modified Capabilities

无。本变更在既有 Research 与本机运行合同上增加产品层能力；不放宽原有认证、owner、质量门、恢复或 Harness 约束，也不替其他 active change 宣布验收完成。

## Impact

- 文档：新增本目录下的 `prd.md`、`design.md`、三个 capability spec、`tasks.md` 与用户红框截图参考。
- 预计实现范围：桌面宿主与打包入口；`frontend/src/features/paper-agent-lab/` 的设计提炼与正式 Research 页面；已有 `interfaces/services/research_desktop_service.py`、Research 阅读/工作区接口；必要的个人记忆 application service、port 和本地存储适配。
- 不在本次文档工作中变更 API、数据库、运行配置、用户数据或已运行的 3107 界面预览。
- 依赖：`paper-agent-app-design-lab` 的视觉结论；`research-local-desktop-readiness` 的正式本机能力与未完成验收项。后者的运行基础不等同于已经有原生窗口 App 或已完成最终发布。
- 桌面宿主与安装工具在实施前做有界验证并记录 ADR；本 PRD 不凭未经验证的体积或性能判断锁定框架。
