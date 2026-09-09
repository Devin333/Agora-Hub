import { expect, test } from "@playwright/test"

test("research composer has one rounded focus surface, grows with drafts and keeps accessible controls", async ({ page }, testInfo) => {
  let intentCalls = 0
  await page.route("**/api/research/intent", route => {
    intentCalls++
    return route.fulfill({ json: { success: true, data: { summary: "Agent 记忆", query: "Agent memory", sources: ["papers"], constraints: {}, clarification: null } } })
  })
  await page.goto("/design-demo")
  await page.getByRole("button", { name: "选择研究模式" }).click()
  await page.getByRole("menuitem").filter({ hasText: "先聊清楚" }).click()
  await page.getByRole("textbox", { name: "向 Agora AI 提问" }).fill("找 Agent 记忆相关论文")
  await page.getByRole("button", { name: "发送问题" }).click()
  await expect(page.getByRole("region", { name: "确认研究计划" })).toBeVisible()

  const transcript = page.getByRole("region", { name: "研究对话内容", exact: true })
  await expect(transcript.locator("header")).toHaveText("找 Agent 记忆相关论文")
  const composer = page.getByRole("form", { name: "继续研究输入区" })
  await expect(composer.locator('label[for="research-follow-up"]')).toHaveCount(0)
  await expect(page.getByText("继续这次研究", { exact: true })).toHaveCount(0)
  const field = composer.getByRole("textbox", { name: "继续这次研究" })
  const plus = composer.getByRole("button", { name: "添加到本次研究" })
  await plus.focus()
  await page.keyboard.press("Tab")
  await expect(field).toBeFocused()
  const appearance = await field.evaluate(element => {
    const style = getComputedStyle(element), parent = getComputedStyle(element.parentElement!)
    return { outline: style.outlineStyle, border: style.borderTopWidth, shadow: style.boxShadow, resize: style.resize, outerOutline: parent.outlineStyle, radius: parent.borderTopLeftRadius }
  })
  expect(appearance).toEqual({ outline: "none", border: "0px", shadow: "none", resize: "none", outerOutline: "solid", radius: "28px" })
  const singleLineHeight = (await field.boundingBox())!.height
  await field.fill("只看有代码的论文")
  await page.keyboard.press("Shift+Enter")
  await page.keyboard.insertText("重点关注长期记忆")
  await expect(field).toHaveValue("只看有代码的论文\n重点关注长期记忆")
  await expect.poll(async () => (await field.boundingBox())!.height).toBeGreaterThan(singleLineHeight)
  expect(intentCalls).toBe(1)

  const longDraft = Array.from({ length: 15 }, (_, index) => `补充条件 ${index + 1}：优先有可复现实验的 Agent 记忆论文。`).join("\n")
  await field.fill(longDraft)
  await expect.poll(async () => (await field.boundingBox())!.height).toBe(180)
  expect(await field.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true)
  await expect(composer.getByRole("button", { name: "发送补充" })).toBeInViewport()
  const composerBox = (await composer.boundingBox())!, transcriptBox = (await transcript.boundingBox())!
  expect(transcriptBox.y + transcriptBox.height).toBeLessThanOrEqual(composerBox.y + 1)

  await page.reload()
  await expect(field).toHaveValue(longDraft)
  await expect.poll(async () => (await field.boundingBox())!.height).toBe(180)
  expect(intentCalls).toBe(1)
  await field.fill("")
  await expect.poll(async () => (await field.boundingBox())!.height).toBe(singleLineHeight)
  await expect(composer.getByRole("button", { name: "发送补充" })).toBeDisabled()
  await plus.click()
  await expect(page.getByRole("menuitem", { name: "添加论文或项目链接" })).toBeInViewport()
  await page.keyboard.press("Escape")
  await expect(plus).toBeFocused()
  await page.keyboard.press("Tab")
  await expect(field).toBeFocused()
  await page.screenshot({ path: testInfo.outputPath("research-composer.png") })
})
