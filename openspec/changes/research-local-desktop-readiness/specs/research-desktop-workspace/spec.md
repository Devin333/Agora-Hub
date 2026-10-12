## ADDED Requirements

### Requirement: Canonical desktop research routes
The system SHALL 在 `local-desktop` profile 提供以 `/` 为首页的正式 Research 入口，统一论文、阅读、分组和报告导航。新历史 SHALL 使用正式路径；既有 `/design-demo` 研究链接 SHALL 通过限定重定向和幂等规范化迁移，保留有效的查询、返回及续读语义，不创建第二套工作区实现。

#### Scenario: Open the local desktop entry
- **WHEN** 已启动本机 profile 的用户打开 `/`
- **THEN** 系统展示真实 Research 工作区和可操作的论文、材料与报告入口
- **AND** 非本次范围的已有页面不被误删除或迁移

#### Scenario: Resume through an old deep link
- **WHEN** 用户打开旧论文/分组链接或包含旧路径的历史记录
- **THEN** 系统定位到对应正式页面并保留经过验证的 query/定位参数
- **AND** 非法外部 returnTo 不产生开放重定向，重复规范化不改变数据语义

### Requirement: Local account lifecycle
The system SHALL 复用真实本地用户名密码和 first-user bootstrap 完成初始化、登录、会话恢复、到期与退出；公共 OTP/OAuth 配置 SHALL NOT 成为本机账号使用前置条件。账号记录与秘密 SHALL NOT 由 demo fixture 或固定口令生成。Bootstrap/login 等既有认证入口 SHALL 允许无会话调用，但仍校验认可 origin、输入、频率限制和适用的初始化状态；其例外 SHALL NOT 扩展到私有业务读写。

#### Scenario: Initialize the first account
- **WHEN** 未初始化的本机实例接收合法的本地初始化请求
- **THEN** 通过既有 auth application service 原子创建首次账号并建立会话
- **AND** 后续 bootstrap 不能覆盖或重建该账号

#### Scenario: Expired session with unsaved edits
- **WHEN** 正在编辑工作区的会话到期或退出
- **THEN** 私有请求被拒绝，界面提供重新登录方式并保留原 owner 可恢复输入
- **AND** 切换账号后不显示或自动导入上一账号的输入与缓存

### Requirement: Profile constrained session cookies
The system SHALL 只在显式 `local-desktop` profile、确定的 loopback bind/origin 和 HTTP 模式下允许本机会话的非 Secure cookie。HttpOnly、SameSite、有效期及撤销 SHALL 始终保留；其他 profile SHALL 保持其 HTTPS/Secure 要求，不能通过不可信 Host/forwarded header 获得本机例外。

#### Scenario: Production build on the configured local origin
- **WHEN** 用户在确定的 `http://127.0.0.1` origin 使用本机 production build 登录
- **THEN** 浏览器可以保存并续用受约束的会话，刷新后仍能访问本人工作区

#### Scenario: Non-loopback configuration requests the exception
- **WHEN** 配置绑定非 loopback 地址或不可信请求声称来自本机
- **THEN** 启动或认证校验拒绝本机 HTTP 例外
- **AND** 系统不通过关闭鉴权使该请求成功

### Requirement: Server enforced research ownership
The system SHALL 在 BFF/API/application service 验证真实用户会话与 workspace、run、artifact、私有论文和报告 owner。服务 token SHALL NOT 自动赋予浏览器共享管理员身份；用户提交的 user/tenant/namespace 字段 SHALL NOT 覆盖可信身份。业务写操作 SHALL 校验有效会话与同源；bootstrap/login 仅适用既定认证入口例外，直接 API 或 rewrite 访问 SHALL 受同等授权约束。

#### Scenario: Access another owner through the API
- **WHEN** 用户提交其他 owner 的 workspace/run/artifact 身份或伪造 scope 字段
- **THEN** 服务端拒绝请求且不返回对方内容或产物路径

#### Scenario: Bypass the page guard
- **WHEN** 未认证客户端直接调用私有 BFF、rewrite 或后端读写接口
- **THEN** 请求被对应服务授权边界拒绝，不能依赖 React guard 才阻止访问

#### Scenario: Cross-origin mutation
- **WHEN** 有 cookie 的浏览器从非认可 origin 发起写请求
- **THEN** 请求在执行 mutation 前被拒绝

### Requirement: Revisioned workspace persistence and explicit imports
The system SHALL 沿用已有 owner 与 revision/CAS 持久化分组、材料、草稿、会话、笔记和所需活动状态。只有服务端确认后 SHALL 显示已保存；冲突 SHALL 保留未同步编辑。Guest/legacy localStorage 导入 SHALL 是用户明确发起、幂等且限制 owner 的操作。

#### Scenario: Two tabs edit the same workspace
- **WHEN** 第二个标签页用过期 revision 保存
- **THEN** 返回冲突并保留其未同步修改，允许重新读取和明确重试
- **AND** 不静默覆盖第一个标签页的已提交数据

#### Scenario: Import guest material into an account
- **WHEN** 已登录用户显式选择导入自己的 guest 材料
- **THEN** 导入经现有校验与 CAS 持久化，重试不重复插入同一材料
- **AND** 单纯登录或切换账号不会自动合并资料

### Requirement: Revision aware reading continuity
The system SHALL 将需要续用的笔记、选区、章节/PDF 页和报告位置持久化到原有 application owner，并将定位关联到论文来源修订。浏览器或服务重启 SHALL 能恢复已提交状态；失效定位 SHALL 提示原因和可用落点。

#### Scenario: Resume after restarting the services
- **WHEN** 用户保存阅读位置及笔记、关闭浏览器并重启前后端后再次登录
- **THEN** 同一 owner 的材料与笔记可读取，并定位到原来源的正确章节或 PDF 页

#### Scenario: Source revision invalidates the locator
- **WHEN** 重新打开时来源修订已变化且原 locator 不再有效
- **THEN** 系统说明修订变化并进入安全可读位置，不声称完成精确恢复

### Requirement: Real source entry and import recovery
The system SHALL 使用真实 source/search/import application 路径处理搜索、arXiv/DOI/GitHub 引用及 PDF。已有 source policy、上传限制、解析隔离和 publication 检查 SHALL 保留。输入、import/run 身份及可用重试 SHALL 在失败后可恢复；GitHub 来源 SHALL NOT 导致执行仓库代码。

#### Scenario: Import and read a real PDF
- **WHEN** 已登录用户上传符合限制的真实 PDF 且所需解析能力可用
- **THEN** 系统展示真实导入状态并仅在既有质量/发布条件满足后打开可读内容
- **AND** 结果与真实 import/source/artifact 身份关联

#### Scenario: Required parser is unavailable
- **WHEN** 所选解析路径依赖的组件不可用或来源拒绝访问
- **THEN** 系统保留输入与导入身份，说明稳定原因和恢复步骤
- **AND** 不伪造正文、不静默绕过 isolation、不把重试按钮当作已完成导入

### Requirement: Accessible and truthful desktop interaction
The system SHALL 在 1366×768、1920×1080 与 125% 缩放下提供可用的关键流程、键盘焦点和状态提示；加载、空白、错误、不可用、待保存与冲突 SHALL 有明确表现。生产页面 SHALL NOT 填充假业务结果或模拟进度。

#### Scenario: Keyboard use while a request fails
- **WHEN** 用户通过键盘提交后端失败的请求
- **THEN** 控件保持可访问、输入保留、错误可感知且重试有明确操作

#### Scenario: No matching source data
- **WHEN** 真实 API 返回空结果
- **THEN** 页面提供真实空状态与修改条件入口，不插入 fixture 或示例论文冒充结果
