## Why

首页自动分流会把找项目的问题送到论文，提交还需要二次点击，三个模块没有接收问题。用户返回后无法继续此前研究，需要把简洁入口接成真实、可恢复的使用路径。

## What Changes

- 默认自动模式，手动选择优先；明确意图直接导航，歧义在输入框附近轻量确认。
- 四个模块接收原始问题：论文、项目、社区真实检索，报告进入可保存、继续编辑并整理资料的报告准备页。用户明确选择 AI 生成后端后续单独开发。
- 本机最近研究、单条继续入口、删除和清空；保存草稿、筛选和滚动位置。
- 模式相关示例、结果导向卡片说明、桌面间距及键盘/输入法/加载反馈。
- 保留紫色玻璃风格、Ask. Discover.、居中提问框、2×2 模块；报告请求失败不再显示伪造结果。

## Capabilities

### New Capabilities
- `portal-research-entry`: 用户首页提问、跨模块上下文、最近研究和可访问交互。

### Modified Capabilities

无。

## Impact

Frontend portal、papers、projects、community、reports 及现有研究任务 API。复用现有 React、Next.js、Radix 和真实后端；不引入 LLM 路由或云端历史同步，不修改 Harness 的执行权限和质量门禁。
