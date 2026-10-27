import { expect, test, type Page, type Request } from "@playwright/test"

const papers = [
  ["memgpt", "MemGPT: Towards LLMs as Operating Systems"],
  ["generative-agents", "Generative Agents: Interactive Simulacra of Human Behavior"],
  ["rag", "Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks"],
  ["lost-middle", "Lost in the Middle: How Language Models Use Long Contexts"],
  ["react", "ReAct: Synergizing Reasoning and Acting in Language Models"],
  ["attention", "Attention Is All You Need"],
] as const

function trackApiRequests(page: Page) {
  const requests: Array<{ method: string; url: string }> = []
  const record = (request: Request) => {
    if (new URL(request.url()).pathname.startsWith("/api/")) {
      requests.push({ method: request.method(), url: request.url() })
    }
  }
  page.on("request", record)
  return requests
}

async function expectNoHorizontalOverflow(page: Page) {
  const sizes = await page.evaluate(() => ({
    viewport: window.innerWidth,
    document: document.documentElement.scrollWidth,
    body: document.body.scrollWidth,
    root: document.querySelector<HTMLElement>("[data-testid='paper-agent-lab']")?.scrollWidth ?? 0,
    rootClient: document.querySelector<HTMLElement>("[data-testid='paper-agent-lab']")?.clientWidth ?? 0,
  }))
  expect(sizes.document).toBeLessThanOrEqual(sizes.viewport)
  expect(sizes.body).toBeLessThanOrEqual(sizes.viewport)
  expect(sizes.root).toBeLessThanOrEqual(sizes.rootClient)
}

async function openPaper(page: Page, paper: (typeof papers)[number]) {
  await page.getByRole("button", { name: `选择论文：${paper[1]}` }).click()
}

test("loads the isolated six-paper workspace with no auth, history, memory, or other API request", async ({ page }) => {
  const apiRequests = trackApiRequests(page)

  await page.goto("/paper-agent-lab")

  const root = page.getByTestId("paper-agent-lab")
  await expect(root).toBeVisible()
  await expect(root).toHaveAttribute("data-theme", "light")
  await expect(page.getByRole("complementary", { name: "论文工作台导航" })).toBeVisible()
  await expect(page.getByLabel("论文列表")).toBeVisible()
  await expect(page.getByRole("heading", { name: papers[0][1] })).toBeVisible()
  await expect(page.getByRole("button", { name: /^选择论文：/ })).toHaveCount(papers.length)
  await expect(page.getByRole("button", { name: "搜索研究和模块" })).toHaveCount(0)
  await expect(page.getByRole("link", { name: "Agora Hub Research" })).toHaveCount(0)

  for (const [, title] of papers) {
    await expect(page.getByRole("button", { name: `选择论文：${title}` })).toBeVisible()
  }

  await page.waitForTimeout(100)
  expect(apiRequests).toEqual([])
})

test("honors initial query state, canonicalizes invalid state, and keeps browser history reversible", async ({ page }) => {
  const apiRequests = trackApiRequests(page)
  await page.goto("/paper-agent-lab?theme=dark&paper=rag&view=all&tab=details&focus=false&assistant=true")

  // `?theme=` is applied once as the app-wide appearance and dropped from the canonical URL.
  await expect(page.getByTestId("paper-agent-lab")).toHaveAttribute("data-theme", "dark")
  await expect(page).toHaveURL("/paper-agent-lab?paper=rag&view=all&tab=details&focus=false&assistant=true")
  await expect(page.getByRole("heading", { name: papers[2][1] })).toBeVisible()
  await expect(page.getByRole("tab", { name: "详情" })).toHaveAttribute("aria-selected", "true")
  await expect(page.getByRole("button", { name: "关闭论文助手", exact: true })).toBeVisible()

  await page.goto("/paper-agent-lab?paper=rag&view=all&tab=details&focus=false&assistant=false")
  await page.getByRole("button", { name: "使用浅色主题" }).click()
  await expect(page.getByTestId("paper-agent-lab")).toHaveAttribute("data-theme", "light")
  await expect(page).not.toHaveURL(/theme=/)
  await openPaper(page, papers[5])
  await expect(page).toHaveURL(/paper=attention/)

  await page.goBack()
  await expect(page).toHaveURL(/paper=rag/)
  await expect(page.getByRole("heading", { name: papers[2][1] })).toBeVisible()
  await page.goForward()
  await expect(page).toHaveURL(/paper=attention/)
  await expect(page.getByRole("heading", { name: papers[5][1] })).toBeVisible()

  await page.goto("/paper-agent-lab?style=gallery&mode=app&theme=sepia&paper=missing&view=recent&tab=full&focus=1&assistant=yes")
  await expect(page).toHaveURL("/paper-agent-lab?paper=memgpt&view=all&tab=overview&focus=false&assistant=false")
  await expect(page.getByTestId("paper-agent-lab")).toHaveAttribute("data-theme", "light")
  await expect(page.getByRole("heading", { name: papers[0][1] })).toBeVisible()
  expect(apiRequests).toEqual([])
})

test("the theme switch follows the system by default and writes the app-wide appearance", async ({ page }) => {
  const apiRequests = trackApiRequests(page)
  await page.emulateMedia({ colorScheme: "dark" })
  await page.goto("/paper-agent-lab")
  const root = page.getByTestId("paper-agent-lab")
  await expect(root).toHaveAttribute("data-theme", "dark")
  await expect(page.getByRole("button", { name: "跟随系统主题" })).toHaveAttribute("aria-pressed", "true")
  await page.emulateMedia({ colorScheme: "light" })
  await expect(root).toHaveAttribute("data-theme", "light")

  await page.getByRole("button", { name: "使用深色主题" }).click()
  await expect(root).toHaveAttribute("data-theme", "dark")
  await page.reload()
  await expect(root).toHaveAttribute("data-theme", "dark")
  await expect(page.getByRole("button", { name: "使用深色主题" })).toHaveAttribute("aria-pressed", "true")
  expect(await root.evaluate(element => getComputedStyle(element).backgroundColor)).toBe("rgb(22, 22, 22)")
  expect(apiRequests).toEqual([])
})

test("searches, reports no result honestly, filters topics, and sorts the fixed catalogue", async ({ page }) => {
  const apiRequests = trackApiRequests(page)
  await page.goto("/paper-agent-lab")
  const search = page.getByPlaceholder("搜索标题、作者或标签")

  await search.fill("Patrick Lewis")
  await expect(page.getByRole("button", { name: /^选择论文：/ })).toHaveCount(1)
  await expect(page.getByRole("button", { name: `选择论文：${papers[2][1]}` })).toBeVisible()

  await search.fill("不存在的量子香蕉论文")
  await expect(page.getByText("此视图暂无论文")).toBeVisible()
  await expect(page.getByText(/清除搜索条件|更短的关键词/)).toBeVisible()
  await expect(page.getByRole("button", { name: /^选择论文：/ })).toHaveCount(0)
  await expect(page.getByLabel("空论文阅读区")).toBeVisible()
  await expect(page.getByLabel(`${papers[0][1]} 阅读区`)).toHaveCount(0)

  await search.fill("")
  await page.getByRole("button", { name: "基础模型" }).click()
  await expect(page.getByRole("button", { name: /^选择论文：/ })).toHaveCount(1)
  await expect(page.getByRole("button", { name: `选择论文：${papers[5][1]}` })).toBeVisible()

  await page.getByRole("button", { name: /^全部论文/ }).click()
  await page.locator("select").selectOption("title")
  await expect(page.getByRole("listitem").first()).toContainText(papers[5][1])
  expect(apiRequests).toEqual([])
})

test("keeps saved, unread, and per-paper notes as honest page-only state", async ({ page }) => {
  const apiRequests = trackApiRequests(page)
  await page.goto("/paper-agent-lab")

  await page.getByLabel(`${papers[0][1]} 阅读区`)
    .getByRole("button", { name: `收藏：${papers[0][1]}` }).click()
  await page.getByRole("button", { name: /^我的收藏/ }).click()
  await expect(page.getByRole("button", { name: `选择论文：${papers[0][1]}` })).toBeVisible()
  await page.getByLabel(`${papers[0][1]} 阅读区`)
    .getByRole("button", { name: `取消收藏：${papers[0][1]}` }).click()
  await expect(page.getByText("此视图暂无论文")).toBeVisible()
  await expect(page.getByLabel(`${papers[0][1]} 阅读区`)).toHaveCount(0)

  await page.getByRole("button", { name: /^全部论文/ }).click()
  await openPaper(page, papers[0])
  await page.getByRole("button", { name: /^未读/ }).click()
  await page.getByRole("button", { name: "标为已读" }).click()
  await expect(page.getByRole("button", { name: `选择论文：${papers[0][1]}` })).toHaveCount(0)

  await page.getByRole("button", { name: /^全部论文/ }).click()
  await openPaper(page, papers[0])
  await page.getByRole("tab", { name: "笔记" }).click()
  const note = page.getByRole("textbox", { name: "本篇笔记" })
  await note.fill("MemGPT：关注分层记忆与上下文调度。")
  await openPaper(page, papers[1])
  await page.getByRole("tab", { name: "笔记" }).click()
  await expect(page.getByRole("textbox", { name: "本篇笔记" })).toHaveValue("")
  await page.getByRole("textbox", { name: "本篇笔记" }).fill("Generative Agents：关注反思与计划。")
  await openPaper(page, papers[0])
  await page.getByRole("tab", { name: "笔记" }).click()
  await expect(page.getByRole("textbox", { name: "本篇笔记" })).toHaveValue("MemGPT：关注分层记忆与上下文调度。")

  await page.reload()
  await expect(page.getByRole("textbox", { name: "本篇笔记" })).toHaveValue("")
  expect(apiRequests).toEqual([])
})

test("supports keyboard search, focus reading, assistant suggestions, and paper-scoped questions", async ({ page }) => {
  const apiRequests = trackApiRequests(page)
  await page.goto("/paper-agent-lab")
  await page.getByRole("button", { name: "使用深色主题" }).click()
  await expect(page.getByTestId("paper-agent-lab")).toHaveAttribute("data-theme", "dark")
  await page.getByRole("button", { name: "使用浅色主题" }).click()
  await expect(page.getByTestId("paper-agent-lab")).toHaveAttribute("data-theme", "light")

  await page.keyboard.press(process.platform === "darwin" ? "Meta+k" : "Control+k")
  await expect(page.getByPlaceholder("搜索标题、作者或标签")).toBeFocused()

  await page.getByRole("button", { name: "专注阅读" }).click()
  await expect(page.getByRole("button", { name: "退出专注阅读", exact: true })).toBeVisible()
  await expect(page).toHaveURL(/focus=true/)
  await page.getByRole("button", { name: "退出专注阅读", exact: true }).click()
  await expect(page).toHaveURL(/focus=false/)

  await page.getByRole("button", { name: "继续追问" }).click()
  const input = page.getByRole("textbox", { name: "向论文助手提问" })
  await expect(input).toBeFocused()
  await expect(input).not.toHaveValue("")
  await input.fill("这篇论文如何管理上下文边界？")
  await page.getByRole("button", { name: "提交预览问题" }).click()
  await expect(page.getByText("这篇论文如何管理上下文边界？")).toBeVisible()
  await expect(page.getByText(/问题未发送.*未连接研究服务/)).toBeVisible()
  await expect(page.getByText(/模型回答|AI 回答|生成答案/)).toHaveCount(0)

  await page.getByRole("button", { name: "关闭论文助手", exact: true }).click()
  await openPaper(page, papers[1])
  await page.getByRole("button", { name: "打开论文助手" }).click()
  await expect(page.getByText("这篇论文如何管理上下文边界？")).toHaveCount(0)
  await expect(page.getByRole("textbox", { name: "向论文助手提问" })).toHaveValue("")

  await page.getByRole("button", { name: "关闭论文助手", exact: true }).click()
  await openPaper(page, papers[0])
  await page.getByRole("button", { name: "打开论文助手" }).click()
  await expect(page.getByText("这篇论文如何管理上下文边界？")).toBeVisible()

  await page.getByRole("button", { name: "关闭论文助手", exact: true }).click()
  await expect(page.getByRole("textbox", { name: "向论文助手提问" })).toHaveCount(0)
  await expect(page).toHaveURL(/assistant=false/)
  expect(apiRequests).toEqual([])
})

test("assistant is non-modal on desktop and modal with an inert background on narrow screens", async ({ page }) => {
  const apiRequests = trackApiRequests(page)
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto("/paper-agent-lab")

  await page.getByRole("button", { name: "打开论文助手" }).click()
  const desktopAssistant = page.locator('[role="complementary"][aria-label="论文助手"]')
  await expect(desktopAssistant).toBeVisible()
  await expect(desktopAssistant).not.toHaveAttribute("aria-modal", "true")
  await page.getByRole("tab", { name: "详情" }).click()
  await expect(page.getByRole("tab", { name: "详情" })).toHaveAttribute("aria-selected", "true")

  await page.getByRole("button", { name: "关闭论文助手", exact: true }).click()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto("/paper-agent-lab?paper=memgpt&view=all&tab=overview&focus=false&assistant=true")
  const narrowAssistant = page.getByRole("dialog", { name: "论文助手" })
  await expect(narrowAssistant).toBeVisible()
  await expect(narrowAssistant).toHaveAttribute("aria-modal", "true")
  expect(await page.getByLabel("论文列表").evaluate((element) => Boolean(element.closest("[inert]")))).toBe(true)
  await expect(page.getByRole("button", { name: "关闭论文助手", exact: true })).toBeFocused()
  expect(apiRequests).toEqual([])
})

for (const theme of ["light", "dark"] as const) {
  for (const width of [390, 768, 1440]) {
    test(`${theme} workspace has no horizontal overflow at ${width}px`, async ({ page }) => {
      const apiRequests = trackApiRequests(page)
      await page.setViewportSize({ width, height: width === 390 ? 844 : 900 })
      await page.goto(`/paper-agent-lab?theme=${theme}&paper=memgpt&view=all&tab=overview&focus=false&assistant=false`)

      await expect(page.getByTestId("paper-agent-lab")).toHaveAttribute("data-theme", theme)
      await expectNoHorizontalOverflow(page)
      expect(apiRequests).toEqual([])
    })
  }
}

test("narrow layout can enter a paper and return to the list", async ({ page }) => {
  const apiRequests = trackApiRequests(page)
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto("/paper-agent-lab")

  await openPaper(page, papers[2])
  await expect(page.getByRole("heading", { name: papers[2][1] })).toBeVisible()
  await page.getByRole("button", { name: "返回论文列表" }).click()
  await expect(page.getByRole("button", { name: `选择论文：${papers[2][1]}` })).toBeVisible()
  await expectNoHorizontalOverflow(page)
  expect(apiRequests).toEqual([])
})