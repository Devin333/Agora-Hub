import { expect, test, type Page } from "@playwright/test"

async function openHome(page: Page) {
  await page.goto("/design-demo")
  await expect(page.getByRole("button", { name: "选择研究模式" })).toBeEnabled()
}

test("desktop composer is centered beside the sidebar and keeps the 2x2 modules", async ({ page }) => {
  for (const viewport of [{ width: 1440, height: 900 }, { width: 1366, height: 768 }]) {
    await page.setViewportSize(viewport); await openHome(page)
    const navigation = page.getByRole("navigation", { name: "主导航", exact: true })
    await expect(navigation.getByRole("button", { name: "登录", exact: true })).toHaveCount(0)
    await expect(navigation.getByRole("link", { name: "研究模块", exact: true })).toHaveCount(0)
    await expect(navigation.getByRole("button", { name: "搜索研究和模块" })).toHaveCount(0)
    const box = await page.getByRole("region", { name: "研究提问框" }).boundingBox()
    const workspace = await page.getByLabel("研究工作区", { exact: true }).boundingBox()
    expect(Math.abs(box!.x + box!.width / 2 - (workspace!.x + workspace!.width / 2))).toBeLessThan(2)
    expect(Math.abs(box!.y + box!.height / 2 - viewport.height / 2)).toBeLessThan(2)
    const cards = page.locator("#modules > a")
    await expect(cards).toHaveCount(4)
    const positions = await cards.evaluateAll(items => items.map(item => item.getBoundingClientRect().toJSON()))
    expect(positions[0].y).toBe(positions[1].y); expect(positions[2].y).toBe(positions[3].y); expect(positions[2].y).toBeGreaterThan(positions[0].y)
    const plus = await page.getByRole("button", { name: "添加到本次研究" }).boundingBox()
    const mode = await page.getByRole("button", { name: "选择研究模式" }).boundingBox()
    expect(plus!.x).toBeLessThan(mode!.x)
  }
  await page.getByRole("button", { name: "选择研究模式" }).focus(); await page.keyboard.press("ArrowDown")
  await expect(page.getByRole("menu")).toBeVisible(); await page.keyboard.press("Escape")
  await expect(page.getByRole("menu")).not.toBeVisible()
  const input = page.getByRole("textbox", { name: "向 Agora AI 提问" })
  await input.fill("找 Agent 的相关研究")
  await page.getByRole("button", { name: "添加到本次研究" }).click()
  await page.getByRole("menuitem", { name: "添加论文或项目链接" }).click()
  await page.getByRole("textbox", { name: "链接", exact: true }).fill("https://github.com/openai/codex")
  await page.getByRole("button", { name: "添加", exact: true }).click()
  await expect(input).toHaveValue("找 Agent 的相关研究")
  await expect(page.getByLabel("本次研究选中的资料")).toContainText("openai/codex")
  await page.reload(); await expect(input).toHaveValue("找 Agent 的相关研究")
  await expect(page.getByLabel("本次研究选中的资料")).toContainText("openai/codex")
  await expect(page.getByRole("button", { name: "开始研究", exact: true })).toHaveCount(0)
  await page.getByRole("button", { name: "登录同步研究", exact: true }).click()
  await expect(page.getByRole("dialog")).toBeVisible(); await page.keyboard.press("Escape")
  await expect(input).toHaveValue("找 Agent 的相关研究")
  await page.getByRole("button", { name: "收起研究侧栏" }).click()
  await page.getByRole("button", { name: "登录同步研究", exact: true }).click()
  await expect(page.getByRole("dialog")).toBeVisible(); await page.keyboard.press("Escape")
  await expect(input).toHaveValue("找 Agent 的相关研究")
})

test("account actions stay in the sidebar when expanded or collapsed", async ({ page }) => {
  let logoutCalls = 0
  await page.route("**/api/auth/session", route => route.fulfill({ json: { success: true, data: { session: { sessionId: "header-test-session", expiresAt: "2099-12-31T00:00:00Z", user: { userId: "header-test-user", username: "Researcher", role: "user" } } } } }))
  await page.route("**/api/research/history", route => route.fulfill({ json: { success: true, data: { visits: [], groups: [], revision: 0 } } }))
  await page.route("**/api/auth/logout", route => {
    logoutCalls++
    return route.fulfill({ json: { success: true, data: { revoked: true } } })
  })
  await openHome(page)
  const sidebar = page.getByRole("complementary", { name: "研究历史侧栏" })
  const accountMenu = sidebar.getByRole("button", { name: "账户菜单" })
  await expect(accountMenu).toHaveText(/Researcher/)
  await expect(page.getByRole("navigation", { name: "主导航", exact: true }).getByRole("button", { name: "账户菜单" })).toHaveCount(0)
  await accountMenu.click()
  await expect(page.getByRole("menuitem", { name: "退出登录" })).toBeVisible()
  await page.keyboard.press("Escape")
  await page.getByRole("button", { name: "收起研究侧栏" }).click()
  await expect(accountMenu).toBeVisible()
  await accountMenu.click()
  await page.getByRole("menuitem", { name: "退出登录" }).click()
  await expect(sidebar.getByRole("button", { name: "登录同步研究" })).toBeVisible()
  expect(logoutCalls).toBe(1)
})

test("plan choices, partial-source recovery, continuation and reload work in a browser", async ({ page }) => {
  let intentCalls = 0, paperCalls = 0, projectCalls = 0
  await page.route("**/api/research/intent", route => {
    intentCalls++
    return route.fulfill({ json: { success: true, data: { summary: "Agent 的任务完成能力", query: "Agent", sources: ["papers", "projects"], constraints: {}, clarification: null } } })
  })
  await page.route("**/api/research/search", route => {
    const source = route.request().postDataJSON().source
    if (source === "projects" && ++projectCalls === 1) return route.fulfill({ status: 503, json: { success: false, error: { message: "项目来源暂时不可用" } } })
    if (source === "papers") paperCalls++
    return route.fulfill({ json: { success: true, data: { source, total: 1, moreHref: source === "papers" ? "/design-demo/papers?q=Agent" : "/projects?q=Agent", results: [{ id: source, kind: source, title: source === "papers" ? "用于交互测试的论文" : "用于交互测试的项目", description: "浏览器测试固定数据", source: source === "papers" ? "arXiv" : "GitHub", url: source === "papers" ? "https://arxiv.org/abs/2605.22343" : "https://github.com/openai/codex" }] } } })
  })
  await openHome(page)
  await page.getByRole("button", { name: "选择研究模式" }).click(); await page.getByRole("menuitem").filter({ hasText: "先聊清楚" }).click()
  await page.getByRole("textbox", { name: "向 Agora AI 提问" }).fill("找 Agent 的论文和项目")
  await page.getByRole("button", { name: "发送问题" }).click()
  const confirm = page.getByRole("region", { name: "确认研究计划" })
  await expect(confirm).toBeVisible()
  await confirm.getByRole("button", { name: "论文", exact: true }).click()
  expect(paperCalls + projectCalls).toBe(0)
  await confirm.getByRole("button", { name: "都看看" }).click(); await confirm.getByRole("button", { name: "开始查找" }).click()
  await expect(page.getByRole("link", { name: "打开原文" })).toBeVisible()
  await page.getByRole("button", { name: "重试开源项目" }).click(); await expect(page.getByRole("link", { name: "打开 GitHub" })).toBeVisible()
  expect(paperCalls).toBe(1); expect(projectCalls).toBe(2)
  await page.getByRole("textbox", { name: "继续这次研究" }).fill("只看可以本地运行的")
  await page.getByRole("button", { name: "发送补充" }).click(); await expect(confirm).toBeVisible()
  await confirm.getByRole("button", { name: "开始查找" }).click()
  await expect(page.getByRole("link", { name: "打开 GitHub" })).toHaveCount(2)
  await page.reload(); await expect(page.getByRole("link", { name: "打开 GitHub" })).toHaveCount(2)
  expect(intentCalls).toBe(2)
  await expect(page.getByRole("button", { name: "继续研究：找 Agent 的论文和项目", exact: true })).toHaveCount(1)
})

test("follow-up composer stays at the workspace bottom while research scrolls above it", async ({ page }) => {
  await page.route("**/api/research/intent", route => route.fulfill({ json: { success: true, data: { summary: "Agent 记忆", query: "Agent memory", sources: ["papers"], constraints: {}, clarification: null } } }))
  await page.route("**/api/research/search", route => route.fulfill({ json: { success: true, data: { source: "papers", total: 10, results: Array.from({ length: 10 }, (_, index) => ({ id: `layout-paper-${index}`, kind: "papers", title: `布局回归论文 ${index + 1}`, description: "用于验证长结果列表不会遮挡追问输入框。", source: "arXiv", url: `https://arxiv.org/abs/2605.${String(22343 + index)}` })) } } }))
  await openHome(page)
  await page.getByRole("button", { name: "选择研究模式" }).click()
  await page.getByRole("menuitem").filter({ hasText: "先聊清楚" }).click()
  await page.getByRole("textbox", { name: "向 Agora AI 提问" }).fill("找 Agent 记忆相关论文")
  await page.getByRole("button", { name: "发送问题" }).click()
  await expect(page.getByRole("region", { name: "确认研究计划" })).toBeVisible()
  const composer = page.getByRole("form", { name: "继续研究输入区" })
  const transcript = page.getByRole("region", { name: "研究对话内容", exact: true })
  const followUp = page.getByRole("textbox", { name: "继续这次研究" })
  const assertDocked = async () => {
    const workspaceBox = (await page.getByLabel("研究工作区", { exact: true }).boundingBox())!
    const composerBox = (await composer.boundingBox())!
    const transcriptBox = (await transcript.boundingBox())!
    expect(Math.abs(composerBox.y + composerBox.height - workspaceBox.y - workspaceBox.height)).toBeLessThan(2)
    expect(Math.abs(composerBox.x + composerBox.width / 2 - workspaceBox.x - workspaceBox.width / 2)).toBeLessThan(2)
    expect(transcriptBox.y + transcriptBox.height).toBeLessThanOrEqual(composerBox.y + 1)
    await expect(followUp).toBeInViewport()
  }
  for (const viewport of [{ width: 1280, height: 1274 }, { width: 1440, height: 900 }, { width: 1366, height: 768 }]) {
    await page.setViewportSize(viewport)
    await assertDocked()
  }
  await followUp.fill("还有 RAG 相关的")
  await page.getByRole("button", { name: "收起研究侧栏" }).click()
  await assertDocked()
  await page.getByRole("button", { name: "展开研究侧栏" }).click()
  await page.getByRole("button", { name: "开始查找", exact: true }).click()
  await expect(transcript.getByRole("article")).toHaveCount(10)
  const beforeScroll = (await composer.boundingBox())!
  await transcript.focus()
  await page.keyboard.press("Control+End")
  const lastLink = transcript.getByRole("link", { name: "打开原文" }).last()
  await expect(lastLink).toBeInViewport()
  await assertDocked()
  expect((await composer.boundingBox())!.y).toBe(beforeScroll.y)
  const lastLinkBox = (await lastLink.boundingBox())!
  expect(lastLinkBox.y + lastLinkBox.height).toBeLessThan(beforeScroll.y)
  await expect(followUp).toHaveValue("还有 RAG 相关的")
  await composer.getByRole("button", { name: "添加到本次研究" }).click()
  await expect(page.getByRole("menuitem", { name: "添加论文或项目链接" })).toBeInViewport()
  await page.keyboard.press("Escape")
  await page.reload()
  await expect(followUp).toHaveValue("还有 RAG 相关的")
  await expect(lastLink).toBeInViewport()
  await assertDocked()
})

test("report preparation retains the existing editable workflow", async ({ page }) => {
  await openHome(page)
  await page.getByRole("textbox", { name: "向 Agora AI 提问" }).fill("整理 Agent 研究报告")
  await page.getByRole("button", { name: "发送问题", exact: true }).click()
  await expect(page).toHaveURL(/\/reports\?.*compose=1/)
  await expect(page.getByRole("textbox", { name: "研究主题", exact: true })).toHaveValue("整理 Agent 研究报告")
  await page.getByRole("textbox", { name: "研究范围" }).fill("比较方法和可验证的证据")
  await page.getByRole("textbox", { name: "分析笔记" }).fill("先核对实验，再记录结论。")
  await page.getByRole("button", { name: "保存草稿", exact: true }).first().click()
  await expect(page).toHaveURL(/draft=/)
  await page.reload()
  await expect(page.getByRole("textbox", { name: "研究范围" })).toHaveValue("比较方法和可验证的证据")
  await expect(page.getByRole("textbox", { name: "分析笔记" })).toHaveValue("先核对实验，再记录结论。")
})
