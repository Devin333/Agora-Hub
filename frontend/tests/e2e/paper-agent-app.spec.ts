import { createHash } from "node:crypto"
import { expect, test, type Page, type Locator } from "@playwright/test"
import type { ResearchPendingOperation } from "../../src/lib/research/workspace-items"

const generation = `gen_${"a".repeat(32)}`
const fingerprint = `sha256:${"b".repeat(64)}`

type Workspace = {
  visits: unknown[]
  groups: Array<{ id: string; name: string; createdAt: number; updatedAt: number }>
  materials?: Array<Record<string, unknown>>
  readerNotebooks?: unknown[]
  pendingOperations?: ResearchPendingOperation[]
}

async function installFixture(page: Page, initialWorkspace: Workspace, options: { historyConflict?: boolean; memoryStatus?: number; readerReady?: boolean; memoryRemoteAllowed?: boolean; operationResult?: Record<string, unknown> } = {}) {
  let workspace = structuredClone(initialWorkspace)
  let revision = 0
  let memoryRecords: Array<Record<string, unknown>> = []
  const memoryCreates: Array<Record<string, unknown>> = []
  const operationRequests: Array<{ method: string; requestId: string; generation: string | null }> = []
  const memoryPolicyMethods: string[] = []
  const memoryMutations: Array<{ method: string; path: string; memoryId: string; body: Record<string, unknown> }> = []
  await page.route("**/api/auth/session", route => route.fulfill({ json: { success: true, data: { initialized: true, bootstrapAvailable: false, session: { sessionId: "session-alice", expiresAt: "2099-12-31T00:00:00Z", user: { userId: "alice", username: "alice", role: "user" } } } } }))
  await page.route("**/api/research/history", async route => {
    if (route.request().method() === "PUT") {
      if (options.historyConflict) return route.fulfill({ status: 409, json: { success: false, error: { code: "revision_conflict", message: "Revision changed" } } })
      const body = JSON.parse(route.request().postData() ?? "{}")
      const next = { ...body }
      delete next.revision
      workspace = next
      revision += 1
    }
    return route.fulfill({ json: { success: true, data: { ...workspace, revision } } })
  })
  await page.route("**/api/research/desktop/identity", route => route.fulfill({ json: { success: true, data: { owner: { userId: "alice", tenantId: null, memoryNamespace: "research:user:alice" }, generation, requestContract: { fingerprint: "sha256-canonical-json-v1", requestId: "stable request identity", notFoundRetry: "query original request" } } } }))
  await page.route("**/api/research/desktop/capabilities", route => route.fulfill({ json: { success: true, data: { generation, capabilities: [] } } }))
  if (options.operationResult) {
    await page.route("**/api/research/desktop/operations/*", route => {
      const url = new URL(route.request().url())
      const request = { method: route.request().method(), requestId: url.pathname.split("/").at(-1) ?? "", generation: url.searchParams.get("generation") }
      operationRequests.push(request)
      if (request.method !== "GET" || request.requestId !== options.operationResult?.requestId || request.generation !== options.operationResult?.generation) {
        return route.fulfill({ status: 409, json: { success: false, error: { code: "operation_identity_mismatch", message: "Original request identity required" } } })
      }
      return route.fulfill({ json: { success: true, data: options.operationResult } })
    })
  }
  await page.route("**/api/research/personal-memory**", async route => {
    if (options.memoryStatus) return route.fulfill({ status: options.memoryStatus, json: { success: false, error: { code: "memory_unavailable", message: "Memory BFF unavailable" } } })
    const url = new URL(route.request().url())
    if (url.pathname.endsWith("/policy")) {
      memoryPolicyMethods.push(route.request().method())
      const allowed = options.memoryRemoteAllowed === true
      return route.fulfill({ json: { policy: { allow_remote_processing: allowed, remote_processing_permissions: { model: allowed, tool: allowed, embedding: allowed }, revision: 1, created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z" } } })
    }
    if (url.pathname.endsWith("/info")) return route.fulfill({ json: { info: { storage: "server_sqlite", status: "available", remote_processing_default: false, remote_processing_auto_enabled: false } } })
    if (url.pathname.endsWith("/export")) return route.fulfill({ json: { export: { schema_version: "1", exported_at: "2026-10-14T00:00:00Z", records: memoryRecords, policy: { allow_remote_processing: false, revision: 1 }, notices: [] } } })
    if (route.request().method() === "POST" && url.pathname.endsWith("/personal-memory")) {
      const body = JSON.parse(route.request().postData() ?? "{}")
      memoryCreates.push(body)
      const record = { memory_id: `pmem_${"1".repeat(32)}`, content: body.content, category: body.category, source_refs: body.source_refs, status: body.creation === "confirmed" ? "active" : "candidate", revision: 1, origin: body.creation === "confirmed" ? "user_confirmed" : "model_candidate", confirmed_at: body.creation === "confirmed" ? "2026-10-14T00:00:00Z" : null, disabled_at: null, deleted_at: null, created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z" }
      memoryRecords = [record]
      return route.fulfill({ json: { success: true, data: { record } } })
    }
    const memoryId = url.pathname.match(/\/(pmem_[0-9a-f]{32})(?:\/|$)/)?.[1]
    if (memoryId && route.request().method() !== "GET") {
      const body = JSON.parse(route.request().postData() ?? "{}")
      memoryMutations.push({ method: route.request().method(), path: url.pathname, memoryId, body })
      const current = memoryRecords.find(item => item.memory_id === memoryId)
      if (!current) return route.fulfill({ status: 404, json: { error: { code: "not_found", message: "not found" } } })
      if (body.revision !== current.revision) return route.fulfill({ status: 409, json: { error: { code: "revision_conflict", message: "Expected current memory revision" } } })
      const status = url.pathname.endsWith("/status") ? body.status : route.request().method() === "DELETE" ? "deleted" : current.status
      const updated = { ...current, ...((route.request().method() === "PATCH") ? body : {}), status, revision: Number(current.revision) + 1, updated_at: "2026-10-14T00:01:00Z" }
      memoryRecords = memoryRecords.map(item => item.memory_id === memoryId ? updated : item)
      return route.fulfill({ json: { success: true, data: { record: updated } } })
    }
    const statuses = url.searchParams.getAll("status")
    const filtered = statuses.length ? memoryRecords.filter(item => statuses.includes(String(item.status))) : memoryRecords.filter(item => item.status !== "deleted")
    const cursor = Number(url.searchParams.get("cursor") ?? "0")
    const limit = Number(url.searchParams.get("limit") ?? "50")
    const records = filtered.slice(cursor, cursor + limit)
    const next = cursor + records.length < filtered.length ? String(cursor + records.length) : null
    return route.fulfill({ json: { records, total: filtered.length, next_cursor: next } })
  })
  await page.route("**/api/research/desktop/papers/*/reader", route => {
    const paperId = decodeURIComponent(new URL(route.request().url()).pathname.split("/").at(-2) ?? "paper")
    const paper = { id: paperId, slug: paperId, title: paperId === "paper-2" ? "Second Paper" : "First Paper", abstract: `Verified abstract for ${paperId}`, abstractSnippet: `Verified abstract for ${paperId}`, authors: ["Real Author"], publishedAt: "2026-10-01", tags: [], topics: [], taskRefs: [], methodRefs: [], paperUrl: `https://example.org/${paperId}`, isPublished: false }
    const data = options.readerReady ? {
      paper,
      reader: {
        paper,
        sections: [{ id: "introduction", paperId, title: "Introduction", level: 1, pageStart: 1, textExcerpt: "Source-backed structured text for monochrome reader verification.", summary: "Verified source excerpt.", sectionType: "introduction" }],
        aiSummary: null, readerNotes: [], relatedPapers: [], relatedProjects: [], relatedNews: [],
        quality: { paperId, pdfAvailable: false, textExtracted: true, summaryAvailable: false, implementationVerified: false, benchmarkVerified: false, evidenceCoverage: 0.6 },
      },
      state: "ready", sourceUrl: `https://example.org/${paperId}`, sourceRevision: fingerprint, sourceFingerprint: fingerprint, capabilities: { ask: true, analyze: true },
    } : { paper, reader: null, state: "unavailable", reasonCode: "document_not_ready", sourceUrl: `https://example.org/${paperId}`, sourceRevision: fingerprint, sourceFingerprint: fingerprint, capabilities: { ask: false, analyze: false } }
    return route.fulfill({ json: { success: true, data } })
  })
  return { workspace: () => workspace, revision: () => revision, memoryCreates, operationRequests, memoryPolicyMethods, memoryMutations, setMemoryRecords: (records: Array<Record<string, unknown>>) => { memoryRecords = records }, memoryRecords: () => memoryRecords }
}

function material(id: string, title: string, patch: Record<string, unknown> = {}) {
  return { id: `paper:${id}`, groupId: null, kind: "paper", title, url: `https://example.org/${id}`, referenceId: id, readerHref: `/papers/${id}/read`, notes: `Metadata for ${title}`, favorite: false, readingStatus: "unread", tags: [], createdAt: 1, updatedAt: 1, ...patch }
}

function memoryRecord(index: number, status: "candidate" | "active" | "disabled" = "active") {
  return { memory_id: `pmem_${index.toString(16).padStart(32, "0")}`, content: `memory record ${index}`, category: "research_interest", source_refs: [{ source_type: "user_input", source_ref: `fixture:${index}`, label: `Fixture ${index}` }], status, revision: 1, origin: status === "candidate" ? "model_candidate" : "user_confirmed", confirmed_at: status === "candidate" ? null : "2026-10-14T00:00:00Z", disabled_at: status === "disabled" ? "2026-10-14T00:00:00Z" : null, deleted_at: null, created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z" }
}

async function assertSingleLineControl(control: Locator) {
  await expect(control).toBeInViewport({ ratio: 1 })
  const geometry = await control.evaluate(element => {
    const box = element.getBoundingClientRect()
    const textNodes = [...element.childNodes].filter(node => node.nodeType === Node.TEXT_NODE && node.textContent?.trim())
    const textRects = textNodes.flatMap(node => {
      const range = document.createRange()
      range.selectNodeContents(node)
      return [...range.getClientRects()].map(rect => ({ width: rect.width, height: rect.height, left: rect.left, right: rect.right }))
    })
    const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)
    return {
      text: element.textContent?.trim(),
      lineCount: textRects.length,
      inside: textRects.every(rect => rect.left >= box.left && rect.right <= box.right),
      unclipped: element.clientWidth >= element.scrollWidth,
      hitVisible: Boolean(hit && element.contains(hit)),
    }
  })
  expect(geometry.lineCount, `${geometry.text} 不得逐字换行`).toBe(1)
  expect(geometry.inside, `${geometry.text} 不得越过控制边界`).toBe(true)
  expect(geometry.unclipped, `${geometry.text} 不得裁切`).toBe(true)
  expect(geometry.hitVisible, `${geometry.text} 必须可触达`).toBe(true)
}

async function assertLibraryNavigationLayout(page: Page) {
  const nav = page.getByRole("navigation", { name: "论文库视图" })
  await expect(nav).toBeVisible()
  if ((await nav.boundingBox())!.x < 0) {
    await page.locator("#paper-reader-main").getByRole("button", { name: "打开导航", exact: true }).click()
  }
  await expect(nav.locator("[data-paper-agent-nav-label]").first()).toBeInViewport({ ratio: 1 })
  const geometry = await nav.locator("[data-paper-agent-nav-label]").evaluateAll(labels => labels.map(label => {
    const button = label.closest("button")!
    const content = label.parentElement!
    const icon = content.querySelector("svg")!
    const count = button.querySelector("small")!
    const labelBox = label.getBoundingClientRect()
    const buttonBox = button.getBoundingClientRect()
    const iconBox = icon.getBoundingClientRect()
    const countBox = count.getBoundingClientRect()
    const range = document.createRange()
    range.selectNodeContents(label)
    const textBox = range.getBoundingClientRect()
    const contentBox = content.getBoundingClientRect()
    const hit = document.elementFromPoint((textBox.left + textBox.right) / 2, (textBox.top + textBox.bottom) / 2)
    return {
      label: label.textContent,
      labelWidth: labelBox.width,
      textWidth: textBox.width,
      unclipped: label.clientWidth >= label.scrollWidth && textBox.left >= labelBox.left - 1 && textBox.right <= labelBox.right + 1,
      insideContent: textBox.right <= contentBox.right + 1,
      insideButton: labelBox.left >= buttonBox.left && countBox.right <= buttonBox.right,
      iconVisible: iconBox.width >= 15 && iconBox.right <= labelBox.left,
      countSeparated: labelBox.right <= countBox.left,
      hitVisible: Boolean(hit && button.contains(hit)),
    }
  }))
  expect(geometry.map(item => item.label)).toEqual(["全部论文", "未读", "我的收藏"])
  for (const item of geometry) {
    expect(item.labelWidth, `${item.label} 标签容器不能落入图标的 18px 列`).toBeGreaterThan(24)
    expect(item.labelWidth, `${item.label} 完整文字必须容纳在标签内`).toBeGreaterThanOrEqual(item.textWidth - 1)
    expect(item.unclipped, `${item.label} 不能隐藏或截断完整文字`).toBe(true)
    expect(item.insideContent, `${item.label} 必须在导航内容边界内`).toBe(true)
    expect(item.insideButton, `${item.label} 和计数必须在按钮内`).toBe(true)
    expect(item.iconVisible, `${item.label} 图标不能被压缩或裁切`).toBe(true)
    expect(item.countSeparated, `${item.label} 计数不能挤占文字列`).toBe(true)
    expect(item.hitVisible, `${item.label} 必须实际显示且不被其他元素覆盖`).toBe(true)
  }
  return geometry
}

test("真实空库、主题、键盘与离线边界", async ({ page }) => {
  await installFixture(page, { visits: [], groups: [] })
  await page.goto("/paper-agent")
  await expect(page.getByTestId("paper-agent-app")).toBeVisible()
  await expect(page.getByText("真实论文库为空")).toBeVisible()
  await expect(page.getByText(/六篇样本|公共 catalog/)).toBeVisible()
  await page.getByRole("button", { name: "使用深色主题" }).click()
  // The workbench follows the app-wide appearance, which ThemeBridge writes to the document root.
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark")
  await expect(page.getByRole("button", { name: "使用深色主题" })).toHaveAttribute("aria-pressed", "true")
  await page.keyboard.press(process.platform === "darwin" ? "Meta+k" : "Control+k")
  await expect(page.getByRole("searchbox", { name: "筛选本地论文库" })).toBeFocused()
  await page.context().setOffline(true)
  await page.evaluate(() => window.dispatchEvent(new Event("offline")))
  await expect(page.getByText("离线模式")).toBeVisible()
  await page.getByRole("textbox", { name: "在线论文搜索" }).fill("grounded RAG")
  await expect(page.getByRole("button", { name: "搜索真实来源" })).toBeDisabled()
  await page.context().setOffline(false)
})



test("窄屏抽屉约束键盘焦点并在 Escape 后归还入口", async ({ page }) => {
  await page.setViewportSize({ width: 800, height: 900 })
  await installFixture(page, { visits: [], groups: [] })
  await page.goto("/paper-agent")

  const opener = page.getByRole("button", { name: "打开导航", exact: true }).first()
  await opener.click()
  const navigation = page.getByRole("complementary", { name: "论文工作台导航" })
  const close = navigation.getByRole("button", { name: "关闭导航" })
  await expect(close).toBeFocused()
  await page.keyboard.press("Shift+Tab")
  await expect(navigation.getByRole("button", { name: "使用深色主题" })).toBeFocused()
  await page.keyboard.press("Tab")
  await expect(close).toBeFocused()
  await page.keyboard.press("Escape")
  await expect(opener).toBeFocused()

  await page.keyboard.press("ControlOrMeta+k")
  await expect(page.getByRole("searchbox", { name: "筛选本地论文库" })).toBeFocused()
  await page.keyboard.press("Escape")
  await expect(opener).toBeFocused()
})

test("本地排序、仅元数据状态与重开导入 request 可核对", async ({ page }) => {
  const requestId = "11111111-1111-4111-8111-111111111111"
  // Flat import semantic fields are in canonical sorted-key order.
  const semantic: ResearchPendingOperation["semantic"] = {
    filename: "reopen.pdf", kind: "import", sizeBytes: 2048, sourceFingerprint: fingerprint, sourceRevision: fingerprint,
  }
  const operationFingerprint = `sha256:${createHash("sha256").update(JSON.stringify(semantic)).digest("hex")}`
  const pendingImport: ResearchPendingOperation = {
    requestId, generation, fingerprint: operationFingerprint, kind: "import", status: "unknown",
    semantic,
    sourceRevision: fingerprint, createdAt: 1, updatedAt: 4,
  }
  const fixture = await installFixture(page, {
    visits: [], groups: [], pendingOperations: [pendingImport],
    materials: [
      material("metadata", "Zeta metadata", { readerHref: undefined, updatedAt: 1 }),
      material("newer", "Beta recent", { createdAt: 2, updatedAt: 4 }),
      material("alpha", "Alpha title", { createdAt: 3, updatedAt: 3 }),
    ],
  }, {
    operationResult: {
      requestId, generation, fingerprint: operationFingerprint, kind: "import", status: "passed",
      updatedAt: "2026-10-15T00:00:00Z", importId: "import-reopen", paperId: "paper-reopen",
      sourceRevision: fingerprint, recoverable: false, automaticRetryAllowed: false,
      result: { filename: "reopen.pdf", paperId: "paper-reopen", sourceRevision: fingerprint, sourceFingerprint: fingerprint },
    },
  })
  await page.goto("/paper-agent")

  await expect(page.getByTestId("paper-agent-app")).toBeVisible()
  await expect(page.getByRole("heading", { name: "仅元数据，全文尚未入库" })).toBeVisible()
  await expect(page.getByText(/元数据已保存.*不等于全文可离线阅读/)).toBeVisible()
  await expect(page.getByRole("link", { name: "打开论文来源" })).toHaveAttribute("href", "https://example.org/metadata")

  const labels = page.getByRole("button", { name: /^选择论文：/ })
  await expect(labels.nth(0)).toHaveAttribute("aria-label", "选择论文：Beta recent")
  await page.getByRole("combobox", { name: "论文排序" }).selectOption("title-asc")
  await expect(labels.nth(0)).toHaveAttribute("aria-label", "选择论文：Alpha title")
  await expect(labels.nth(1)).toHaveAttribute("aria-label", "选择论文：Beta recent")
  await expect(labels.nth(2)).toHaveAttribute("aria-label", "选择论文：Zeta metadata")

  const imports = page.getByRole("region", { name: "已保存的导入请求" })
  await expect(imports).toContainText("reopen.pdf")
  await expect(imports).toContainText("结果待核对")
  expect(fixture.operationRequests).toEqual([])
  await imports.getByRole("button", { name: "检查已有请求" }).click()
  await expect(imports).toContainText("处理完成")
  await expect(page.getByText(/不会自动重新上传或补发|已由服务端确认完成/)).toBeVisible()
  expect(fixture.operationRequests).toEqual([{ method: "GET", requestId, generation }])
  await expect.poll(() => fixture.workspace().pendingOperations?.find(item => item.requestId === requestId)).toMatchObject({
    requestId, generation, fingerprint: operationFingerprint, semantic, status: "passed",
    paperId: "paper-reopen", importId: "import-reopen", sourceRevision: fingerprint, sourceFingerprint: fingerprint,
  })
})

test("真实 OpenReader 与 ReaderAssistant 在正式壳层跟随 Rakazo 浅色与深色表面", async ({ page }, testInfo) => {
  await installFixture(page, { visits: [], groups: [], materials: [material("paper-1", "First Paper")] }, { readerReady: true })
  await page.setViewportSize({ width: 1440, height: 900 })
  await page.goto("/paper-agent")
  await page.getByRole("button", { name: "使用浅色主题" }).click()

  const readerRoot = page.locator('[data-workspace-appearance]')
  const readerBody = page.getByRole("region", { name: "Open reader paper body" })
  await expect(readerBody).toContainText("Source-backed structured text")
  await expect(readerRoot).toHaveAttribute("data-workspace-appearance", "monochrome-light")
  expect(await readerBody.evaluate(element => getComputedStyle(element).backgroundColor)).toBe("rgb(255, 255, 255)")
  expect(await readerBody.evaluate(element => getComputedStyle(element).color)).toBe("rgb(28, 28, 28)")

  await page.getByRole("button", { name: "打开助手" }).click()
  const assistant = page.getByRole("complementary", { name: "阅读助手" })
  const askInput = page.getByLabel("向这篇论文提问")
  await expect(assistant).toBeVisible()
  expect(await assistant.evaluate(element => getComputedStyle(element).backgroundColor)).toBe("rgb(255, 255, 255)")
  expect(await assistant.evaluate(element => getComputedStyle(element).color)).toBe("rgb(28, 28, 28)")
  expect(await askInput.locator("..").evaluate(element => getComputedStyle(element).backgroundColor)).not.toBe("rgba(0, 0, 0, 0)")
  await assertLibraryNavigationLayout(page)
  const lightScreenshot = testInfo.outputPath("paper-agent-reader-assistant-light.png")
  await page.screenshot({ path: lightScreenshot, animations: "disabled" })
  await testInfo.attach("paper-agent-reader-assistant-light", { path: lightScreenshot, contentType: "image/png" })

  await page.getByRole("button", { name: "使用深色主题" }).click()
  await expect(readerRoot).toHaveAttribute("data-workspace-appearance", "monochrome-dark")
  expect(await readerBody.evaluate(element => getComputedStyle(element).backgroundColor)).toBe("rgb(22, 22, 22)")
  expect(await readerBody.evaluate(element => getComputedStyle(element).color)).toBe("rgb(245, 245, 245)")
  expect(await assistant.evaluate(element => getComputedStyle(element).backgroundColor)).toBe("rgb(22, 22, 22)")
  expect(await assistant.evaluate(element => getComputedStyle(element).color)).toBe("rgb(245, 245, 245)")
  expect(await askInput.evaluate(element => getComputedStyle(element).color)).toBe("rgb(245, 245, 245)")
  await page.getByRole("button", { name: "阅读笔记" }).click()
  const noteInput = page.getByLabel("我的阅读笔记")
  await expect(noteInput).toBeVisible()
  await expect(askInput).not.toBeVisible()
  expect(await noteInput.evaluate(element => getComputedStyle(element).backgroundColor)).not.toBe("rgb(22, 22, 22)")
  expect(await noteInput.evaluate(element => getComputedStyle(element).color)).toBe("rgb(245, 245, 245)")
  await assertLibraryNavigationLayout(page)
  const darkScreenshot = testInfo.outputPath("paper-agent-reader-assistant-dark.png")
  await page.screenshot({ path: darkScreenshot, animations: "disabled" })
  await testInfo.attach("paper-agent-reader-assistant-dark", { path: darkScreenshot, contentType: "image/png" })
})

test("导航完整标签在 1366/1440 与 125% 等效缩放下不截断且嵌套操作保持紧凑", async ({ browser }, testInfo) => {
  for (const physicalWidth of [1366, 1440]) {
    for (const scale of [1, 1.25]) {
      // Browser zoom reduces the CSS viewport and scales CSS pixels at the same time.
      const context = await browser.newContext({
        baseURL: testInfo.project.use.baseURL,
        viewport: { width: Math.round(physicalWidth / scale), height: Math.round(900 / scale) },
        deviceScaleFactor: scale,
      })
      try {
        const page = await context.newPage()
        await installFixture(page, {
          visits: [],
          groups: [{ id: "nav-group", name: "研究分组", createdAt: 1, updatedAt: 1 }],
          materials: [material("paper-1", "First Paper", { groupId: "nav-group", tags: ["复现"] })],
        }, { readerReady: true })
        await page.goto("/paper-agent")
        await expect(page.getByRole("region", { name: "Open reader paper body" })).toContainText("Source-backed structured text")
        await assertLibraryNavigationLayout(page)
        await page.getByRole("button", { name: "使用深色主题" }).click()
        if (scale > 1) await page.getByRole("button", { name: "关闭导航", exact: true }).click()
        await page.getByRole("button", { name: "打开助手" }).click()
        const geometry = await assertLibraryNavigationLayout(page)
        const nav = page.getByRole("navigation", { name: "论文库视图" })
        await expect(nav.getByRole("button", { name: /^研究分组/ })).toBeVisible()
        for (const name of ["改名分组：研究分组", "删除分组：研究分组"]) {
          const action = nav.getByRole("button", { name })
          expect((await action.boundingBox())!.width).toBeCloseTo(28, 3)
          expect(await action.evaluate(element => getComputedStyle(element).display)).toBe("flex")
        }
        expect((await nav.getByRole("button", { name: "创建分组" }).boundingBox())!.width).toBeCloseTo(32, 3)
        const tag = nav.getByRole("button", { name: /^复现/ })
        const tagBox = await tag.boundingBox()
        const navBox = await nav.boundingBox()
        expect(tagBox?.width).toBeGreaterThan(40)
        expect(tagBox!.width).toBeLessThan(navBox!.width / 2)
        expect(await tag.evaluate(element => getComputedStyle(element).display)).toBe("flex")
        const name = `paper-agent-nav-${physicalWidth}-${Math.round(scale * 100)}pct`
        const screenshot = testInfo.outputPath(`${name}.png`)
        await page.screenshot({ path: screenshot, animations: "disabled" })
        await testInfo.attach(name, { path: screenshot, contentType: "image/png" })
        await testInfo.attach(`${name}-geometry`, { body: JSON.stringify(geometry, null, 2), contentType: "application/json" })
        if (scale > 1) {
          await page.getByRole("button", { name: "关闭导航", exact: true }).click()
          await expect(nav).not.toBeInViewport()
          const sidebar = page.getByRole("complementary", { name: "论文工作台导航" })
          const library = page.getByRole("region", { name: "论文列表", exact: true })
          await expect(sidebar).not.toBeInViewport()
          await expect(library).not.toBeInViewport()
          const body = page.getByRole("region", { name: "Open reader paper body" })
          const bodyBox = await body.boundingBox()
          const paragraphBox = await body.locator("[data-paragraph-id]").first().boundingBox()
          expect(bodyBox!.width).toBeGreaterThanOrEqual(700)
          expect(paragraphBox!.width).toBeGreaterThanOrEqual(600)
          const assistantBox = await page.getByRole("complementary", { name: "论文助手" }).boundingBox()
          expect(assistantBox!.x).toBeGreaterThanOrEqual(bodyBox!.x + bodyBox!.width - 1)
          for (const label of ["原文", "概览", "笔记", "详情"]) {
            const tab = page.getByRole("tab", { name: label, exact: true })
            await assertSingleLineControl(tab)
          }
          await assertSingleLineControl(page.getByRole("link", { name: "来源", exact: true }))
          await assertSingleLineControl(page.getByRole("button", { name: "专注", exact: true }))
          await expect(page.getByRole("checkbox", { name: "使用已授权记忆", exact: true })).toBeInViewport({ ratio: 1 })
          await expect(page.getByRole("checkbox", { name: "本次助手任务使用已授权记忆", exact: true })).toBeInViewport({ ratio: 1 })
          expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
          await page.locator("#paper-reader-main").getByRole("button", { name: "收起或打开论文列表" }).click()
          await expect(library).toBeInViewport({ ratio: 1 })
          await page.getByRole("button", { name: "关闭论文列表", exact: true }).click()
          await expect(library).not.toBeInViewport()
          const bodyScreenshot = testInfo.outputPath(`paper-agent-reader-${physicalWidth}-125pct.png`)
          await page.screenshot({ path: bodyScreenshot, animations: "disabled" })
          await testInfo.attach(`paper-agent-reader-${physicalWidth}-125pct`, { path: bodyScreenshot, contentType: "image/png" })
          await testInfo.attach(`paper-agent-reader-${physicalWidth}-125pct-width`, {
            body: JSON.stringify({ viewport: page.viewportSize(), bodyWidth: bodyBox!.width, paragraphWidth: paragraphBox!.width, assistantWidth: assistantBox!.width }),
            contentType: "application/json",
          })
        }
      } finally {
        await context.close()
      }
    }
  }
})

test("窄屏助手保留可触达的每任务记忆选择且不绕过服务端 policy", async ({ page }) => {
  const fixture = await installFixture(page, { visits: [], groups: [], materials: [material("paper-1", "First Paper")] }, { readerReady: true, memoryRemoteAllowed: true })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto("/paper-agent")
  await expect(page.getByTestId("paper-agent-app")).toBeVisible()
  await page.getByRole("button", { name: "打开导航", exact: true }).click()
  await page.getByRole("button", { name: /^个人记忆/ }).click()
  const memory = page.getByRole("dialog", { name: "个人记忆" })
  await expect(memory.getByText("research:user:alice", { exact: true })).toBeVisible()
  await expect(memory.getByRole("button", { name: "已允许", exact: true })).toHaveAttribute("aria-pressed", "true")
  await expect(memory.getByText("策略允许模型、工具和 embedding；本次不用记忆仍优先", { exact: true })).toBeVisible()
  await expect(memory.getByText("模型 允许 · 工具 允许 · embedding 允许", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "关闭个人记忆", exact: true }).click()
  await page.getByRole("button", { name: "关闭导航", exact: true }).click()
  await page.getByRole("button", { name: "打开助手", exact: true }).click()
  const toggle = page.getByRole("checkbox", { name: "本次助手任务使用已授权记忆", exact: true })
  await expect(toggle).toBeInViewport({ ratio: 1 })
  await expect(toggle).toBeEnabled()
  await expect(toggle).not.toBeChecked()
  await toggle.check()
  await expect(page.getByText("本任务已启用受控个人记忆")).toBeVisible()
  await toggle.uncheck()
  await expect(page.getByText("本次不使用个人记忆")).toBeVisible()
  expect(fixture.memoryPolicyMethods.length).toBeGreaterThan(0)
  expect(fixture.memoryPolicyMethods.every(method => method === "GET")).toBe(true)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test("真实论文按 paper 隔离笔记并在窄窗口使用可返回抽屉", async ({ page }) => {
  await installFixture(page, { visits: [], groups: [], materials: [material("paper-1", "First Paper"), material("paper-2", "Second Paper")], readerNotebooks: [{ paperId: "paper-1", sourceRevision: fingerprint, note: "paper one note", question: "", fontSize: 18, sectionId: "", pdfPage: 1, updatedAt: 2 }] })
  await page.setViewportSize({ width: 390, height: 844 }); await page.goto("/paper-agent")
  await page.getByRole("button", { name: "打开论文列表" }).click()
  await page.getByRole("button", { name: "选择论文：First Paper" }).click()
  await page.getByRole("tab", { name: "笔记" }).click(); await expect(page.getByLabel("我的阅读笔记")).toHaveValue("paper one note")
  await page.getByRole("button", { name: "打开论文列表" }).click(); await page.getByRole("button", { name: "选择论文：Second Paper" }).click(); await page.getByRole("tab", { name: "笔记" }).click(); await expect(page.getByLabel("我的阅读笔记")).toHaveValue("")
  await page.getByRole("button", { name: "打开助手" }).click(); await expect(page.getByRole("complementary", { name: "论文助手" })).toBeVisible(); await page.keyboard.press("Escape"); await expect(page.getByRole("complementary", { name: "论文助手" })).toHaveCount(0)
})

test("收藏取消、显式已读、标签与分组均保存回读且不删除论文", async ({ page }) => {
  const fixture = await installFixture(page, { visits: [], groups: [], materials: [material("paper-1", "First Paper")] })
  await page.goto("/paper-agent")
  const card = page.getByRole("button", { name: "选择论文：First Paper" }).locator("..")
  await card.getByRole("button", { name: "收藏", exact: true }).click()
  await expect(card.getByText("待保存")).toBeVisible()
  await expect.poll(() => fixture.revision()).toBeGreaterThan(0)
  await expect(card.getByText("已保存")).toBeVisible()
  expect(fixture.workspace().materials?.[0]).toMatchObject({ favorite: true })
  await page.getByRole("button", { name: /我的收藏/ }).click(); await expect(page.getByRole("button", { name: "选择论文：First Paper" })).toBeVisible()
  await card.getByRole("button", { name: "已收藏" }).click(); await expect.poll(() => fixture.workspace().materials?.[0]?.favorite).toBe(false)
  expect(fixture.workspace().materials).toHaveLength(1); expect(fixture.workspace().materials?.[0]).toMatchObject({ referenceId: "paper-1", notes: "Metadata for First Paper" })
  await page.getByRole("button", { name: /全部论文/ }).click()
  await card.getByRole("button", { name: "标记已读" }).click(); await expect.poll(() => fixture.workspace().materials?.[0]?.readingStatus).toBe("read")
  page.once("dialog", dialog => dialog.accept("RAG, evaluation, rag")); await card.getByRole("button", { name: "标签" }).click(); await expect.poll(() => fixture.workspace().materials?.[0]?.tags).toEqual(["RAG", "evaluation"])
  await page.getByLabel("新分组名称").fill("核心阅读"); await page.getByRole("button", { name: "创建分组" }).click(); await card.getByLabel("设置分组：First Paper").selectOption({ label: "核心阅读" }); await expect.poll(() => fixture.workspace().materials?.[0]?.groupId).not.toBeNull()
  page.once("dialog", dialog => dialog.accept("重点阅读")); await page.getByRole("button", { name: "改名分组：核心阅读" }).click(); await expect(page.getByText("重点阅读", { exact: true }).first()).toBeVisible()
  page.once("dialog", dialog => dialog.accept()); await page.getByRole("button", { name: "删除分组：重点阅读" }).click(); await expect.poll(() => fixture.workspace().materials?.[0]?.groupId).toBeNull(); expect(fixture.workspace().materials).toHaveLength(1)
})

test("IME 确认不误发在线搜索，材料 CAS 冲突逐条显示失败并可重试", async ({ page }) => {
  await installFixture(page, { visits: [], groups: [], materials: [material("paper-1", "First Paper")] }, { historyConflict: true })
  let searchRequests = 0
  await page.route("**/api/research/intent", route => { searchRequests += 1; return route.fulfill({ json: { success: true, data: { summary: "RAG", query: "RAG", sources: ["papers"], constraints: {}, clarification: null } } }) })
  await page.route("**/api/research/search", route => route.fulfill({ json: { success: true, data: { source: "papers", results: [], total: 0 } } }))
  await page.goto("/paper-agent")
  const onlineSearch = page.getByRole("textbox", { name: "在线论文搜索" }); await onlineSearch.focus(); await onlineSearch.evaluate(element => element.dispatchEvent(new CompositionEvent("compositionstart", { bubbles: true, data: "论文" }))); await onlineSearch.fill("论文"); await onlineSearch.press("Enter"); expect(searchRequests).toBe(0); await onlineSearch.evaluate(element => element.dispatchEvent(new CompositionEvent("compositionend", { bubbles: true, data: "论文" }))); await page.getByRole("button", { name: "搜索真实来源" }).click(); await expect.poll(() => searchRequests).toBe(1)
  await page.getByRole("button", { name: "收藏", exact: true }).click()
  await expect(page.getByText("保存失败").first()).toBeVisible({ timeout: 3000 })
  await expect(page.getByRole("button", { name: "重试保存" })).toBeVisible()
  await expect(page.getByText(/revision 冲突|草稿仍保留/).first()).toBeVisible()
})

test("手动确认记忆发送真实结构化 refs，默认导出排除 deleted", async ({ page }) => {
  const fixture = await installFixture(page, { visits: [], groups: [], materials: [material("paper-1", "First Paper")] })
  await page.goto("/paper-agent"); await page.getByRole("button", { name: /个人记忆/ }).click()
  const dialog = page.getByRole("dialog", { name: "个人记忆" }); await expect(dialog).toBeVisible(); await expect(dialog.getByText("服务器账号空间（仅本账号可见）")).toBeVisible()
  await dialog.getByLabel("新个人记忆").fill("优先阅读可复现实验")
  await dialog.getByRole("button", { name: "保存并确认" }).click()
  await expect.poll(() => fixture.memoryCreates.length).toBe(1)
  expect(fixture.memoryCreates[0]).toMatchObject({ creation: "confirmed", source_refs: [{ source_type: "paper", source_ref: "paper-1", label: "First Paper" }] })
  await expect(dialog.getByText(/active/)).toBeVisible()
})

test("个人记忆抽屉显示真实 BFF 错误", async ({ page }) => {
  await installFixture(page, { visits: [], groups: [] }, { memoryStatus: 503 })
  await page.goto("/paper-agent"); await page.getByRole("button", { name: /个人记忆/ }).click()
  await expect(page.getByRole("dialog", { name: "个人记忆" })).toBeVisible(); await expect(page.getByText("research:user:alice")).toBeVisible(); await expect(page.getByText("Memory BFF unavailable")).toBeVisible(); await page.keyboard.press("Escape"); await expect(page.getByRole("dialog", { name: "个人记忆" })).toHaveCount(0)
})



test("桌面退出桥接只上报真实 dirty/active 请求并尊重取消或 drain", async ({ page }) => {
  await page.addInitScript(() => {
    const harness: { updates: Array<Record<string, unknown>>; responses: Array<Record<string, unknown>>; listener?: (request: Record<string, unknown>) => void } = { updates: [], responses: [] }
    Object.assign(window, {
      __paperAgentLifecycle: harness,
      agoraDesktop: {
        version: 1,
        lifecycle: {
          updateState: async (state: Record<string, unknown>) => { harness.updates.push(state); return { accepted: true, revision: state.revision as number } },
          onQuitRequested: (listener: (request: Record<string, unknown>) => void) => { harness.listener = listener; return () => { harness.listener = undefined } },
          respondToQuit: async (response: Record<string, unknown>) => { harness.responses.push(response); return { accepted: true } },
        },
      },
    })
  })
  await installFixture(page, { visits: [], groups: [], materials: [material("paper-1", "First Paper")] })
  let searchRequests = 0
  await page.route("**/api/research/intent", async route => {
    searchRequests += 1
    await new Promise(resolve => setTimeout(resolve, 700))
    await route.fulfill({ json: { success: true, data: { summary: "RAG", query: "RAG", sources: ["papers"], constraints: {}, clarification: null } } })
  })
  await page.route("**/api/research/search", route => route.fulfill({ json: { success: true, data: { source: "papers", results: [], total: 0 } } }))
  await page.goto("/paper-agent")
  const input = page.getByRole("textbox", { name: "在线论文搜索" })
  await input.fill("尚未提交的草稿")
  await expect.poll(() => page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentLifecycle: { updates: Array<{ dirty?: boolean }> } }).__paperAgentLifecycle
    return harness.updates.at(-1)?.dirty
  })).toBe(true)
  page.once("dialog", dialog => dialog.dismiss())
  await page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentLifecycle: { listener?: (request: Record<string, unknown>) => void } }).__paperAgentLifecycle
    harness.listener?.({ requestId: "quit-cancel", reason: "window-close", state: {} })
  })
  await expect.poll(() => page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentLifecycle: { responses: Array<{ decision?: string }> } }).__paperAgentLifecycle
    return harness.responses.at(-1)?.decision
  })).toBe("cancel")

  await input.fill("RAG"); await page.getByRole("button", { name: "搜索真实来源" }).click()
  await expect.poll(() => page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentLifecycle: { updates: Array<{ activeTaskCount?: number }> } }).__paperAgentLifecycle
    return Math.max(...harness.updates.map(item => item.activeTaskCount ?? 0))
  })).toBeGreaterThan(0)
  page.once("dialog", dialog => dialog.accept())
  await page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentLifecycle: { listener?: (request: Record<string, unknown>) => void } }).__paperAgentLifecycle
    harness.listener?.({ requestId: "quit-allow", reason: "app-quit", state: {} })
  })
  await expect.poll(() => page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentLifecycle: { responses: Array<{ decision?: string }> } }).__paperAgentLifecycle
    return harness.responses.at(-1)?.decision
  })).toBe("allow")
  const quitActiveCount = await page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentLifecycle: { responses: Array<{ state?: { activeTaskCount?: number } }> } }).__paperAgentLifecycle
    return harness.responses.at(-1)?.state?.activeTaskCount ?? 0
  })
  expect(quitActiveCount).toBeGreaterThan(0)
  expect(searchRequests).toBe(1)
})



test("在线搜索失败或超时保留原始输入且不自动重发", async ({ page }) => {
  await installFixture(page, { visits: [], groups: [] })
  let requests = 0
  await page.route("**/api/research/intent", async route => {
    requests += 1
    await route.fulfill({ status: 503, json: { success: false, error: { code: "research_timeout", message: "Research provider timed out" } } })
  })
  await page.goto("/paper-agent")
  const input = page.getByRole("textbox", { name: "在线论文搜索" })
  await input.fill("保留这条原始研究问题")
  await page.getByRole("button", { name: "搜索真实来源" }).click()
  await expect(input).toHaveValue("保留这条原始研究问题")
  await expect(page.getByText(/Research provider timed out|检索/).first()).toBeVisible()
  await page.waitForTimeout(800)
  expect(requests).toBe(1)
  await input.fill("用户编辑后的重试问题")
  await expect(input).toHaveValue("用户编辑后的重试问题")
})

test("桌面原生导出接收笔记原文并正确处理取消", async ({ page }) => {
  await page.addInitScript(() => {
    const harness: { requests: Array<Record<string, unknown>> } = { requests: [] }
    Object.assign(window, {
      __paperAgentExport: harness,
      agoraDesktop: {
        version: 1,
        export: {
          save: async (request: Record<string, unknown>) => {
            harness.requests.push(request)
            return { ok: false, code: "cancelled", message: "cancelled" }
          },
        },
      },
    })
  })
  await installFixture(page, { visits: [], groups: [], materials: [material("paper-1", "First Paper")] })
  await page.goto("/paper-agent")
  await page.getByRole("tab", { name: "笔记" }).click()
  await page.getByLabel("我的阅读笔记").fill("Exact native note content")
  await page.getByRole("button", { name: "导出 Markdown" }).click()
  const request = await expect.poll(() => page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentExport: { requests: Array<Record<string, unknown>> } }).__paperAgentExport
    return harness.requests.at(-1)
  })).not.toBeUndefined()
  void request
  const exported = await page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentExport: { requests: Array<{ format?: string; content?: string }> } }).__paperAgentExport
    return harness.requests.at(-1)
  })
  expect(exported).toMatchObject({ format: "md" })
  expect(exported?.content).toContain("Exact native note content")
  expect(exported?.content).toContain("Source revision")
  await expect(page.getByText("已取消笔记导出。")).toBeVisible()

  await page.getByRole("button", { name: /个人记忆/ }).click()
  const memoryDialog = page.getByRole("dialog", { name: "个人记忆" })
  await memoryDialog.getByRole("button", { name: "导出 JSON" }).click()
  const memoryExport = await expect.poll(() => page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentExport: { requests: Array<{ format?: string; content?: string }> } }).__paperAgentExport
    return harness.requests.at(-1)
  })).toMatchObject({ format: "json" })
  void memoryExport
  const memoryRequest = await page.evaluate(() => {
    const harness = (window as unknown as Window & { __paperAgentExport: { requests: Array<{ format?: string; content?: string }> } }).__paperAgentExport
    return harness.requests.at(-1)
  })
  expect(JSON.parse(memoryRequest?.content ?? "{}")).toMatchObject({ schema_version: "1", records: [] })
  await expect(memoryDialog.getByText("已取消个人记忆导出。")).toBeVisible()
})

test("桌面导出 IPC 异常返回未知状态且保留未保存笔记", async ({ page }) => {
  await page.addInitScript(() => {
    Object.assign(window, {
      agoraDesktop: {
        version: 1,
        export: { save: async () => { throw new Error("ipc disconnected") } },
      },
    })
  })
  await installFixture(page, { visits: [], groups: [], materials: [material("paper-1", "First Paper: a very long bilingual title 中文标题")] })
  await page.goto("/paper-agent")
  await page.getByRole("tab", { name: "笔记" }).click()
  const note = page.getByLabel("我的阅读笔记")
  await note.fill("导出失败后必须保留的草稿")
  await page.getByRole("button", { name: "导出 Markdown" }).click()
  await expect(page.getByText(/无法确认桌面导出是否完成/)).toBeVisible()
  await expect(note).toHaveValue("导出失败后必须保留的草稿")
})

test("个人记忆分页后第 51 条仍可编辑、启用和删除", async ({ page }) => {
  const fixture = await installFixture(page, { visits: [], groups: [] })
  const memoryId = `pmem_${(50).toString(16).padStart(32, "0")}`
  const original = memoryRecord(50, "disabled")
  fixture.setMemoryRecords(Array.from({ length: 51 }, (_, index) => memoryRecord(index, index === 50 ? "disabled" : "active")))
  await page.goto("/paper-agent")
  await page.getByRole("button", { name: /个人记忆/ }).click()
  const dialog = page.getByRole("dialog", { name: "个人记忆" })
  await expect(dialog.getByText("已加载 50 / 51 条")).toBeVisible()
  await dialog.getByRole("button", { name: "加载更多" }).click()
  await expect(dialog.getByText("已加载 51 / 51 条")).toBeVisible()
  await dialog.getByLabel("记忆状态筛选").selectOption("disabled")
  await expect(dialog.getByText("已加载 1 / 1 条")).toBeVisible()
  let record = dialog.getByRole("article")
  await expect(record).toHaveCount(1)
  await expect(record).toHaveAttribute("data-status", "disabled")
  await expect(record.getByText("memory record 50", { exact: true })).toBeVisible()
  await record.getByRole("button", { name: "编辑", exact: true }).click()
  const editor = record.getByRole("textbox", { name: "编辑记忆内容", exact: true })
  await expect(editor).toHaveValue("memory record 50")
  await editor.fill("memory record 50 edited")
  await expect(editor).toHaveValue("memory record 50 edited")
  expect(fixture.memoryMutations).toEqual([])
  await record.getByRole("button", { name: "保存修改", exact: true }).click()
  await expect(record.getByText("memory record 50 edited", { exact: true })).toBeVisible()
  await expect(editor).toHaveCount(0)
  await expect(record.getByText("r2", { exact: true })).toBeVisible()
  expect(fixture.memoryMutations).toEqual([
    { method: "PATCH", path: `/api/research/personal-memory/${memoryId}`, memoryId, body: { revision: 1, content: "memory record 50 edited" } },
  ])
  expect(fixture.memoryRecords().find(item => item.memory_id === memoryId)).toMatchObject({
    memory_id: memoryId, status: "disabled", content: "memory record 50 edited", revision: 2, source_refs: original.source_refs,
  })
  await record.getByRole("button", { name: "启用", exact: true }).click()
  await expect.poll(() => fixture.memoryRecords().find(item => item.memory_id === memoryId)).toMatchObject({ status: "active", revision: 3, content: "memory record 50 edited", source_refs: original.source_refs })
  await expect(dialog.getByText("没有符合当前视图的真实记忆记录。")).toBeVisible()
  await dialog.getByLabel("记忆状态筛选").selectOption("active")
  await expect(dialog.getByText("已加载 50 / 51 条")).toBeVisible()
  await dialog.getByRole("button", { name: "加载更多" }).click()
  await expect(dialog.getByText("已加载 51 / 51 条")).toBeVisible()
  record = dialog.getByText("memory record 50 edited", { exact: true }).locator("..")
  await expect(record.getByText("r3", { exact: true })).toBeVisible()
  page.once("dialog", confirm => confirm.accept())
  await record.getByRole("button", { name: "删除", exact: true }).click()
  await expect.poll(() => fixture.memoryRecords().find(item => item.memory_id === memoryId)).toMatchObject({ status: "deleted", revision: 4, content: "memory record 50 edited", source_refs: original.source_refs })
  await expect(dialog.getByText("memory record 50 edited", { exact: true })).toHaveCount(0)
  expect(fixture.memoryMutations).toEqual([
    { method: "PATCH", path: `/api/research/personal-memory/${memoryId}`, memoryId, body: { revision: 1, content: "memory record 50 edited" } },
    { method: "POST", path: `/api/research/personal-memory/${memoryId}/status`, memoryId, body: { revision: 2, status: "active" } },
    { method: "DELETE", path: `/api/research/personal-memory/${memoryId}`, memoryId, body: { revision: 3 } },
  ])
})
