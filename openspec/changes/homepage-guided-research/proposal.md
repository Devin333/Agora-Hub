## Why

首页已有资料、历史和模块入口，但计划模式只会把用户转到模块，缺少澄清、真实查找、结果和继续提问的完整研究过程。用户需要保留简约首页，在同一次研究中说清需求、打开真实论文或项目，并随时回来继续。

## What Changes

- 整理最左侧的“+”菜单，添加文件、链接、资料与保存分组；附件与问题分别保存。
- 区分自动和计划模式，提供最多两轮、一次一个问题的自然语言澄清与轻量确认。
- 引入有界的研究会话：理解、确认、查找、校验、结果或失败；前端反馈来自真实请求状态。
- 同时查询真实论文和项目，展示可核验的来源、原文链接、原生阅读器链接、部分失败和空结果。
- 支持停止、重试、继续提问、历史恢复、分组、账号隔离和精确返回。
- 在当前对话左侧提供紧凑的消息索引，支持问题预览、定位和阅读位置高亮；用户消息气泡统一使用首页紫色和白字。
- 保持桌面紫色玻璃 UI，初始居中提问框及下方 2x2 模块；开始研究后切换到对话与结果视图。

## Capabilities

### New Capabilities
- `homepage-guided-research`: 从简约首页发起、澄清、查找和恢复一段带真实来源的研究会话。

### Modified Capabilities

无；复用现有工作区、资料、阅读器与报告准备能力。

## Impact

- Frontend: homepage composer, guided conversation UI, owned workspace schema/history, same-origin research API proxies and desktop tests.
- Application/API: bounded intent candidate and catalog search services, input/result validation and authenticated material resolution.
- Existing paper/project sources and workspace persistence remain authoritative; no new dependency or fake production result source.
- 社区保留模块入口，报告继续进入可保存的准备页；本次不实现按主题生成报告。
