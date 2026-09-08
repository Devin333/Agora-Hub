## Scope

2026-09-08：重建 /papers/methods、/papers/tasks 两个桌面目录，整合论文发现页三组渐进展开分类与 method/task 组合筛选。移除五个无引用旧目录组件，仅移除被替换目录的旧测试；保留原有十个详情/证据/公开数据约束测试。

## Frontend validation

- npm test：110 files / 584 tests passed。
- npm run typecheck：通过。
- npm run lint：通过，无警告或错误。
- npm run build：成功编译、类型检查和全部静态页面生成；两个新目录均为动态服务端路由。
- git diff --check：通过。
- openspec validate paper-discovery-category-navigation --strict：通过。

## Browser validation

- 实际访问端口 3000 的两个原目录地址，确认浅紫色页头、双栏分类卡片和目录切换。
- 1440×900 与 1280×800 桌面验证，无水平溢出。
- 任务方向默认五项，展开显示全部，选择后收起仍保留选中项。
- 搜索代码生成 → 打开页内详情 → 刷新 → 返回目录，搜索条件保持。
- 未关联分类展示真实空状态，并提供显式名称检索入口，不进入旧版 404 详情。
- 论文侧栏研究方法/任务默认五类，Enter 展开、Space 折叠分组，目录链接可达。
- 构建后重启开发服务并复查方法/任务页面，pageerror 为空。
- 截图保存在 .newsroom/verification/paper-category-navigation/{methods,tasks}.png（本地验证产物，不提交）。

## Required smoke and concurrency boundary

命令：F:/github/NewsRoom/.venv/Scripts/python.exe -m scripts.dev smoke。
在隔离工作树 F:/github/NewsRoom-taxonomy-verification-20260908、基线 29b741f5 执行，exit 0：3152 passed / 23 deselected / 23 warnings；Agent loop succeeded；source validation is_valid=true，error_count=0，warning_count=0。

日志：.newsroom/taxonomy-sidebar-smoke.log。该 smoke 覆盖隔离的后端/架构基线；本次前端变更通过上述前端测试和构建验证。执行期间并行任务提交了 22c36d90，并仍有无关后端修改；此 smoke 不声明覆盖这些并行修改。隔离树使用了主树已有但被忽略的 docs/operations/agent-session-retirement.md，SHA256 73D64BA64A6EE0AF62A33CEBAD904EF59E2334B8F38E5E43297D2C3E634A910F。

## Data boundary

当前真实缓存含 1000 篇公开论文，methodRefs/taskRefs 全为空。目录使用已有 7 个方法和 34 个任务的名称、方向与定义，不使用 catalog 示例计数、趋势、论文、关系。未知真实 refs 自动加入，计数仅来自公开论文且去重；无关联标注项可浏览定义，论文筛选禁用。分类筛选的有数据路径使用测试 fixture 验证，当前运行数据下不能声明已完成方法/任务论文标注。
