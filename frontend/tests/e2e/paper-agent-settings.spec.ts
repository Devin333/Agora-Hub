import { expect, test, type Page } from "@playwright/test"

const generation = `gen_${"a".repeat(32)}`
const configuration = {
  schema: "newsroom.desktop-runtime-settings/v1",
  revision: 2,
  research: {
    llm: { routeId: "writer-primary", provider: "openai-compatible", model: "gpt-current", baseUrl: "https://models.example/v1", credentialConfigured: true, credentialBinding: "windows-credential-manager" },
    source: { provider: "arxiv", preset: "arxiv-default", apiUrl: "https://export.arxiv.org/api/query", timeoutSeconds: 90 },
  },
}

async function installSettingsFixture(page: Page) {
  await page.addInitScript(config => {
    Object.assign(window, {
      __settingsConfirmCount: 0,
      confirm: () => {
        ;(window as typeof window & { __settingsConfirmCount: number }).__settingsConfirmCount += 1
        return false
      },
      agoraDesktop: {
        version: 1,
        config: {
          show: async () => ({ ok: true, state: "CONFIGURED", code: "read", message: "read", configuration: config }),
          preview: async () => ({ ok: false, state: "UNUSED", code: "unused", message: "unused" }),
          apply: async () => ({ ok: false, state: "UNUSED", code: "unused", message: "unused" }),
        },
        credentials: {
          status: async () => ({ ok: true, code: "configured", configured: true }),
          replace: async () => ({ ok: false, code: "cancelled", configured: true, cancelled: true }),
          remove: async () => ({ ok: true, code: "removed", configured: false, changed: true }),
        },
      },
    })
  }, configuration)
  await page.route("**/api/auth/session", route => route.fulfill({ json: { success: true, data: { session: { sessionId: "session-alice", expiresAt: "2099-12-31T00:00:00Z", user: { userId: "alice", username: "alice", role: "user" } } } } }))
  await page.route("**/api/research/history", route => route.fulfill({ json: { success: true, data: { visits: [], groups: [], materials: [], readerNotebooks: [], pendingOperations: [], revision: 0 } } }))
  await page.route("**/api/research/desktop/identity", route => route.fulfill({ json: { success: true, data: { owner: { userId: "alice", tenantId: null, memoryNamespace: "research:user:alice" }, generation, requestContract: { fingerprint: "sha256-canonical-json-v1", requestId: "stable request identity", notFoundRetry: "query original request" } } } }))
  await page.route("**/api/research/desktop/capabilities", route => route.fulfill({ json: { success: true, data: { generation, capabilities: [
    { id: "source", label: "论文来源", required: true, configured: true, reachable: true, verified: true, verifiedAt: "2026-10-14T08:00:00Z", reasonCode: "business_operation_verified", message: "已由受控业务操作验证。", provenance: { verificationKind: "business_operation", operation: "source.search" } },
    { id: "paper-question-answering", label: "论文问答", required: true, configured: true, reachable: false, verified: false, verifiedAt: null, reasonCode: "verification_unknown", message: "已配置，尚无本 generation 的业务验证记录。", provenance: null },
  ] } } }))
  await page.route("**/api/research/personal-memory/info", route => route.fulfill({ json: { info: { storage: "local_sqlite", status: "available", data_root: "C:/Agora", database_path: "C:/Agora/memory.db", remote_processing_default: false, remote_processing_auto_enabled: false } } }))
  await page.route("**/api/research/personal-memory/policy", route => route.fulfill({ json: { policy: { allow_remote_processing: false, revision: 1, created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z" } } }))
}

/** Rakazo dialog surface per resolved theme: `--dialog` background and `--foreground` ink. */
const dialogSurface = {
  light: { background: "rgb(255, 255, 255)", color: "rgb(28, 28, 28)" },
  dark: { background: "rgb(31, 31, 31)", color: "rgb(245, 245, 245)" },
} as const

async function expectDialogAppearance(page: Page, theme: keyof typeof dialogSurface) {
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme)
  const dialog = page.getByRole("dialog", { name: "运行状态、数据边界与诊断" })
  const surface = await dialog.evaluate(element => ({ background: getComputedStyle(element).backgroundColor, color: getComputedStyle(element).color }))
  expect(surface).toEqual(dialogSurface[theme])
  const nav = dialog.getByRole("navigation", { name: "设置分区" })
  await expect(nav.getByRole("button")).toHaveCount(6)
  await expect(nav.locator('[aria-current="true"]')).toHaveCount(1)
  // Status badges keep real status hues instead of a monochrome substitute.
  const badgeColors = await dialog.locator("ul li span").evaluateAll(spans => spans
    .filter(span => /已验证|待业务验证|不可达/.test(span.textContent ?? ""))
    .map(span => getComputedStyle(span).color))
  expect(badgeColors.length).toBeGreaterThan(0)
  for (const color of badgeColors) {
    const [r, g, b] = color.match(/\d+(?:\.\d+)?/g)!.map(Number)
    expect(Math.max(r, g, b) - Math.min(r, g, b), `badge color ${color}`).toBeGreaterThan(24)
  }
}

test("settings dialog follows the Rakazo light and dark surfaces and App Escape confirms dirty close once", async ({ page }) => {
  await installSettingsFixture(page)
  await page.goto("/paper-agent")
  await page.getByRole("button", { name: "使用浅色主题" }).click()

  await page.getByRole("button", { name: "设置与诊断" }).click()
  await expect(page.getByRole("dialog", { name: "运行状态、数据边界与诊断" })).toBeVisible()
  await expectDialogAppearance(page, "light")
  await page.getByRole("button", { name: "关闭设置面板" }).click()

  await page.getByRole("button", { name: "使用深色主题" }).click()
  await page.getByRole("button", { name: "设置与诊断" }).click()
  await expectDialogAppearance(page, "dark")

  const model = page.getByRole("textbox", { name: "模型" })
  await model.fill("gpt-dirty")
  await expect(page.getByText("有待应用修改", { exact: true })).toBeVisible()
  await page.keyboard.press("Escape")
  await expect(page.getByRole("dialog", { name: "运行状态、数据边界与诊断" })).toBeVisible()
  await expect(model).toHaveValue("gpt-dirty")
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __settingsConfirmCount: number }).__settingsConfirmCount)).toBe(1)

  await page.locator('[role="presentation"]').click({ position: { x: 4, y: 4 } })
  await expect(model).toHaveValue("gpt-dirty")
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __settingsConfirmCount: number }).__settingsConfirmCount)).toBe(2)

  await page.getByRole("button", { name: "关闭设置面板" }).click()
  await expect(model).toHaveValue("gpt-dirty")
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __settingsConfirmCount: number }).__settingsConfirmCount)).toBe(3)
})
