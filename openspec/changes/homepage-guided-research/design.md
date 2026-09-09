## Context

现有 `DesignDemoPage`、owned workspace/history、PDF import、paper catalog 与 project service 已提供基础能力。当前计划卡仅回到单模块跳转，无法同时展示两类结果。工作树另有 Harness 编排改动，本次不改动这些文件。

## Goals / Non-Goals

**Goals:** 实现自然语言澄清、明确确认、真实双源查找、可取消和重试、来源直达、后续问题、账号内历史恢复；保留桌面居中玻璃 UI。

**Non-Goals:** 移动端布局、外部登录服务申请、按主题生成报告、通用无限代理循环。

## Decisions

- 用独立的 guided-research hook、schema 和视图承载会话，不继续把业务流程堆进首页组件。初始页保持居中和 2x2 模块；有会话时呈现滚动对话、结果和底部输入框。
- 通过有界应用服务理解请求并查询现有真实目录。LLM 只能输出受验证的意图候选；应用控制允许的来源、条件、最多两轮澄清和请求预算。前端按实际异步请求呈现理解/查找/完成；不生成虚假的阶段、百分比或内部推理。
- 自动模式对明确请求直接执行，计划模式确认后执行。选项只更新选择；“都看看”指定论文和项目两条真实查找。问题和补充保留顺序，条件可显式调整，不擅自放宽。
- 研究会话作为有界、受验证的 `ResearchVisit.conversation` 保存，复用账号 CAS 和游客存储。原始问题、确认、轮次、结果引用、阶段记录与滚动位置属于同一个 visit；旧 visit 无此字段仍正常工作。异步响应须校验 owner 与 request generation，切换账号/研究或停止后不得回写。
- “+”菜单添加链接与文件为独立资料引用，不覆盖问题。保存分组与引用资料严格分离。所有读取私有资料的请求使用当前账号的服务端身份与所有权检查，客户端快照不授予访问权限。
- 结果只使用目录返回的元数据、来源和现有正文就绪信息；内部链接带返回研究的定位信息，外链使用新标签页。部分失败保留成功结果并能单独重试；空结果允许用户选择放宽条件。
- 重试复用同一研究和轮次，不制造重复历史；服务超时有明确错误；取消中止前端等待并使迟到响应无效，后台工作也受服务超时约束。

## Risks / Trade-offs

- The existing standalone ParsePaper path does not own a Graph activity identity for external parsers. Uploaded text-bearing PDFs will use a bounded in-process text parser with source locations and existing quality validation; scans or unusable extraction remain explicit failures. This supplies research context only, and does not publish legacy sections as a formal reader body or bypass external-execution identity admission.

- 会话体积增大 → 限制轮次、单轮结果数量与总存储字节；达到上限时保留已有内容并提示新建研究。
- 来源数据缺失 → 正向条件仅由已知元数据证明；不把未知情况当成符合条件。
- 模型或源服务不可用 → 展示准确失败信息和重试入口，不以伪造内容代替成功。
- 草稿和未结束请求跨账号 → 渲染前 owner 隔离、请求代次校验、原子保存；恢复中断阶段时提示继续，禁止自动重新发起付费工作。

## Migration Plan

新增可选会话字段及严格验证，再部署应用/API 和前端；保存旧快照时保持省略字段语义。旧研究继续原有恢复逻辑。新增流程失败时保留可编辑问题和原有模块入口。

## Open Questions

无需要用户补充的产品决策；真实服务配置及数据覆盖在验证记录中如实报告。
