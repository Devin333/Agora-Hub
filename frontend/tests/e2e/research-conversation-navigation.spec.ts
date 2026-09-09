import { expect, test, type Page } from "@playwright/test"
import type { ResearchWorkspace } from "../../src/lib/research/history-model"

const historyKey = "agora-research-history:v2"

async function assertComposerVisible(page: Page) {
  const workspace = (await page.getByLabel("研究工作区", { exact: true }).boundingBox())!
  const composer = (await page.getByRole("form", { name: "继续研究输入区" }).boundingBox())!
  expect(Math.abs(composer.y + composer.height - workspace.y - workspace.height)).toBeLessThan(2)
  await expect(page.getByRole("textbox", { name: "继续这次研究" })).toBeInViewport()
}

test("message index previews and jumps through questions and answers without changing the research", async ({ page }, testInfo) => {
  let intentCalls = 0, searchCalls = 0
  await page.route("**/api/research/intent", route => {
    intentCalls++
    return route.fulfill({ json: { success: true, data: { summary: "Agent 记忆与工具", query: "Agent memory tools", sources: ["papers"], constraints: {}, clarification: intentCalls === 1 ? { question: "你主要想了解哪方面？", options: ["记忆和工具", "部署方式"] } : null } } })
  })
  await page.route("**/api/research/search", route => {
    searchCalls++
    return route.fulfill({ json: { success: true, data: { source: "papers", total: 8, results: Array.from({ length: 8 }, (_, index) => ({ id: `paper-${index}`, kind: "papers", title: `Agent 资料 ${index + 1}`, description: "用于验证多轮研究中的消息定位和阅读连续性。", source: "arXiv", url: "https://arxiv.org/abs/2605.22343" })) } } })
  })
  await page.goto("/design-demo")
  await page.getByRole("button", { name: "选择研究模式" }).click()
  await page.getByRole("menuitem").filter({ hasText: "先聊清楚" }).click()
  const firstQuestion = "找 Agent 记忆相关的研究"
  await page.getByRole("textbox", { name: "向 Agora AI 提问" }).fill(firstQuestion)
  await page.getByRole("button", { name: "发送问题" }).click()
  await page.getByRole("button", { name: "记忆和工具", exact: true }).click()
  await page.getByRole("button", { name: "开始查找", exact: true }).click()
  const transcript = page.getByRole("region", { name: "研究对话内容", exact: true })
  await expect(transcript.getByRole("article")).toHaveCount(8)
  const followUp = page.getByRole("textbox", { name: "继续这次研究" })
  for (const [index, question] of ["还有 RAG 相关的", "比较一下这些方法适合什么场景"].entries()) {
    await followUp.fill(question)
    await page.getByRole("button", { name: "发送补充" }).click()
    await page.getByRole("button", { name: "开始查找", exact: true }).click()
    await expect(transcript.getByRole("article")).toHaveCount((index + 2) * 8)
  }
  const nav = page.getByRole("navigation", { name: "聊天索引" })
  const entries = nav.getByRole("button")
  await expect(entries).toHaveCount(4)
  await expect(transcript.getByText("继续提问", { exact: true })).toHaveCount(0)
  const ragMessage = transcript.getByText("还有 RAG 相关的", { exact: true })
  await expect(ragMessage).toHaveCSS("background-color", "rgb(124, 58, 237)")
  await expect(ragMessage).toHaveCSS("color", "rgb(255, 255, 255)")
  await followUp.fill("继续比较这些资料")
  const sessionUrl = page.url()
  await entries.nth(2).hover()
  await expect(page.getByRole("tooltip")).toContainText("还有 RAG 相关的")
  await entries.nth(2).click()
  await expect(ragMessage).toBeInViewport()
  await expect(entries.nth(2)).toHaveAttribute("aria-current", "location")
  await page.screenshot({ path: testInfo.outputPath("conversation-index.png") })
  await entries.first().click()
  await expect(transcript.getByRole("heading", { name: firstQuestion, exact: true })).toBeInViewport()
  await expect(entries.first()).toHaveAttribute("aria-current", "location")
  await entries.nth(1).click()
  await expect(transcript.getByText("补充：记忆和工具", { exact: true })).toBeInViewport()

  await transcript.focus()
  await page.keyboard.press("Control+End")
  await expect(entries.last()).toHaveAttribute("aria-current", "location")
  await assertComposerVisible(page)
  await page.getByRole("button", { name: "收起研究侧栏" }).click()
  await assertComposerVisible(page)
  await page.getByRole("button", { name: "展开研究侧栏" }).click()
  await entries.nth(2).click()
  await expect(entries.nth(2)).toHaveAttribute("aria-current", "location")
  await page.waitForTimeout(800)
  await page.reload()
  await expect(entries).toHaveCount(4)
  await expect(entries.nth(2)).toHaveAttribute("aria-current", "location")
  await expect(ragMessage).toBeInViewport()
  await expect(followUp).toHaveValue("继续比较这些资料")
  expect(page.url()).toBe(sessionUrl)
  expect(intentCalls).toBe(4)
  expect(searchCalls).toBe(3)

  await page.getByRole("button", { name: "新研究", exact: true }).click()
  await expect(nav).toHaveCount(0)
  await page.getByRole("textbox", { name: "向 Agora AI 提问" }).fill("找机器人相关论文")
  await page.getByRole("button", { name: "发送问题" }).click()
  await expect(entries).toHaveCount(1)
  await expect(entries.first()).toHaveAccessibleName(/找机器人相关论文/)
  await page.getByRole("button", { name: `继续研究：${firstQuestion}`, exact: true }).click()
  await expect(entries).toHaveCount(4)
  await expect(entries.first()).toHaveAccessibleName(new RegExp(firstQuestion))
  await expect(followUp).toHaveValue("继续比较这些资料")
})

test("a full conversation index supports long previews and keyboard navigation with reduced motion", async ({ page }) => {
  const now = Date.now(), sessionId = "conversation-index-full"
  const questions = Array.from({ length: 20 }, (_, index) => `第 ${index + 1} 个问题：${"比较 Agent 的记忆管理与工具调用。".repeat(16)}`)
  const workspace: ResearchWorkspace = { groups: [], visits: [{
    id: sessionId, module: "papers", question: questions[0], title: "长对话索引", href: `/design-demo/papers?question=${encodeURIComponent(questions[0])}&researchSession=${sessionId}`, scrollY: 0,
    groupId: null, isFavorite: false, createdAt: now, updatedAt: now, deletedAt: null,
    conversation: { version: 1, mode: "plan", draft: "保留我的草稿", materialIds: [], turns: questions.map((question, index) => ({
      id: `turn-${index}`, question, answers: [`第 ${index + 1} 轮补充：关注实际应用`, `第 ${index + 1} 轮补充：需要代码`], phase: "results", failures: [], createdAt: now,
      events: [{ phase: "results", at: now }], searches: [{ source: "papers", total: 1, results: [{ id: `paper-${index}`, kind: "papers", title: `Agent 资料 ${index + 1}`, description: "用于检验有界长会话中的索引定位。", source: "arXiv", url: "https://arxiv.org/abs/2605.22343" }] }],
    })) },
  }] }
  await page.addInitScript(({ key, data }) => { if (!localStorage.getItem(key)) localStorage.setItem(key, JSON.stringify(data)) }, { key: historyKey, data: workspace })
  await page.emulateMedia({ reducedMotion: "reduce" })
  await page.setViewportSize({ width: 1366, height: 768 })
  await page.goto(`/design-demo?researchSession=${sessionId}`)
  const nav = page.getByRole("navigation", { name: "聊天索引" })
  const entries = nav.getByRole("button")
  const transcript = page.getByRole("region", { name: "研究对话内容", exact: true })
  await expect(entries).toHaveCount(60)
  await entries.first().focus()
  await expect(page.getByRole("tooltip")).toContainText("第 1 个问题")
  const preview = (await page.getByRole("tooltip").boundingBox())!
  expect(preview.x).toBeGreaterThanOrEqual(0)
  expect(preview.x + preview.width).toBeLessThanOrEqual(1366)
  await page.keyboard.press("Escape")
  await expect(page.getByRole("tooltip")).toHaveCount(0)
  await page.keyboard.press("End")
  await expect(entries.last()).toBeFocused()
  await expect(entries.last()).toBeInViewport()
  await page.keyboard.press("Enter")
  await expect(transcript.getByText("补充：第 20 轮补充：需要代码", { exact: true })).toBeInViewport()
  await expect(entries.last()).toHaveAttribute("aria-current", "location")
  await assertComposerVisible(page)
  const navBox = (await nav.boundingBox())!
  const transcriptBox = (await transcript.boundingBox())!
  expect(navBox.x + navBox.width).toBeLessThanOrEqual(transcriptBox.x)
  await entries.last().focus()
  await page.keyboard.press("Home")
  await expect(entries.first()).toBeFocused()
  await page.keyboard.press("Enter")
  await expect(transcript.getByRole("heading", { level: 1 })).toBeInViewport()
  await expect(entries.first()).toHaveAttribute("aria-current", "location")
  await expect(page.getByRole("textbox", { name: "继续这次研究" })).toHaveValue("保留我的草稿")
})
