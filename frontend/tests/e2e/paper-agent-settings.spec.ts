import { expect, test, type Page } from "@playwright/test"

const generation = `gen_${"a".repeat(32)}`
const modelCredential = {
  credential: { configured: true, status: "verified", providerId: "deepseek", model: "deepseek-chat", keyHint: "a1b2", revision: 2, updatedAt: "2026-10-14T08:00:00Z", verifiedAt: "2026-10-14T08:05:00Z" },
  catalog: { providers: [{ id: "deepseek", label: "DeepSeek", baseUrl: "https://api.deepseek.com", defaultModel: "deepseek-chat", models: [{ id: "deepseek-chat", label: "DeepSeek Chat" }] }] },
}
const settingsDialog = "账号、模型与数据授权"

async function installSettingsFixture(page: Page) {
  await page.addInitScript(() => {
    Object.assign(window, {
      __settingsConfirmCount: 0,
      confirm: () => {
        ;(window as typeof window & { __settingsConfirmCount: number }).__settingsConfirmCount += 1
        return false
      },
    })
  })
  await page.route("**/api/auth/session", route => route.fulfill({ json: { success: true, data: { initialized: true, bootstrapAvailable: false, session: { sessionId: "session-alice", expiresAt: "2099-12-31T00:00:00Z", user: { userId: "alice", username: "alice", role: "user" } } } } }))
  await page.route("**/api/research/history", route => route.fulfill({ json: { success: true, data: { visits: [], groups: [], materials: [], readerNotebooks: [], pendingOperations: [], revision: 0 } } }))
  await page.route("**/api/research/desktop/identity", route => route.fulfill({ json: { success: true, data: { owner: { userId: "alice", tenantId: null, memoryNamespace: "research:user:alice" }, generation, requestContract: { fingerprint: "sha256-canonical-json-v1", requestId: "stable request identity", notFoundRetry: "query original request" } } } }))
  await page.route("**/api/research/desktop/capabilities", route => route.fulfill({ json: { success: true, data: { generation, capabilities: [
    { id: "source", label: "论文来源", required: true, configured: true, reachable: true, verified: true, verifiedAt: "2026-10-14T08:00:00Z", reasonCode: "business_operation_verified", message: "已由受控业务操作验证。", provenance: { verificationKind: "business_operation", operation: "source.search" } },
    { id: "paper-question-answering", label: "论文问答", required: true, configured: true, reachable: false, verified: false, verifiedAt: null, reasonCode: "verification_unknown", message: "已配置，尚无本 generation 的业务验证记录。", provenance: null },
  ] } } }))
  await page.route("**/api/v1/account/model-credential", route => route.fulfill({ json: { success: true, data: modelCredential } }))
  await page.route("**/api/research/personal-memory/info", route => route.fulfill({ json: { info: { storage: "server_sqlite", status: "available", remote_processing_default: false, remote_processing_auto_enabled: false } } }))
  await page.route("**/api/research/personal-memory/policy", route => route.fulfill({ json: { policy: { allow_remote_processing: false, revision: 1, created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z" } } }))
}

/** Rakazo dialog surface per resolved theme: `--dialog` background and `--foreground` ink. */
const dialogSurface = {
  light: { background: "rgb(255, 255, 255)", color: "rgb(28, 28, 28)" },
  dark: { background: "rgb(31, 31, 31)", color: "rgb(245, 245, 245)" },
} as const

async function expectDialogAppearance(page: Page, theme: keyof typeof dialogSurface) {
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme)
  const dialog = page.getByRole("dialog", { name: settingsDialog })
  const surface = await dialog.evaluate(element => ({ background: getComputedStyle(element).backgroundColor, color: getComputedStyle(element).color }))
  expect(surface).toEqual(dialogSurface[theme])
  const nav = dialog.getByRole("navigation", { name: "设置分区" })
  // A regular user sees account, model, capability and network sections; never account administration.
  await expect(nav.getByRole("button")).toHaveCount(4)
  await expect(nav.getByRole("button", { name: "账号管理" })).toHaveCount(0)
  await expect(nav.locator('[aria-current="true"]')).toHaveCount(1)
  // Status badges keep real status hues instead of a monochrome substitute.
  // Status inks are color-mix() tokens, which compute to `color(srgb …)`; paint each one
  // on a canvas to read it back as 0–255 sRGB channels whatever its CSS notation.
  const badgeColors = await dialog.locator("ul li span").evaluateAll(spans => {
    const context = document.createElement("canvas").getContext("2d", { willReadFrequently: true })!
    return spans
      .filter(span => /已验证|待业务验证|不可达|待配置/.test(span.textContent ?? ""))
      .map(span => {
        const css = getComputedStyle(span).color
        context.clearRect(0, 0, 1, 1)
        context.fillStyle = css
        context.fillRect(0, 0, 1, 1)
        const [r, g, b] = context.getImageData(0, 0, 1, 1).data
        return { css, r, g, b }
      })
  })
  expect(badgeColors.length).toBeGreaterThan(0)
  for (const { css, r, g, b } of badgeColors) {
    expect(Math.max(r, g, b) - Math.min(r, g, b), `badge color ${css}`).toBeGreaterThan(24)
  }
}

test("settings dialog follows the Rakazo light and dark surfaces and every close path confirms an unsaved key once", async ({ page }) => {
  await installSettingsFixture(page)
  await page.goto("/paper-agent")
  await page.getByRole("button", { name: "使用浅色主题" }).click()

  await page.getByRole("button", { name: "设置", exact: true }).click()
  await expect(page.getByRole("dialog", { name: settingsDialog })).toBeVisible()
  await expectDialogAppearance(page, "light")
  await page.getByRole("button", { name: "关闭设置面板" }).click()

  await page.getByRole("button", { name: "使用深色主题" }).click()
  await page.getByRole("button", { name: "设置", exact: true }).click()
  await expectDialogAppearance(page, "dark")

  // A typed but unsaved API key is the dialog's draft; every close path must ask once.
  const model = page.getByLabel("API Key")
  await model.fill("sk-unsaved-draft")
  await page.keyboard.press("Escape")
  await expect(page.getByRole("dialog", { name: settingsDialog })).toBeVisible()
  await expect(model).toHaveValue("sk-unsaved-draft")
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __settingsConfirmCount: number }).__settingsConfirmCount)).toBe(1)

  await page.locator('[role="presentation"]').click({ position: { x: 4, y: 4 } })
  await expect(model).toHaveValue("sk-unsaved-draft")
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __settingsConfirmCount: number }).__settingsConfirmCount)).toBe(2)

  await page.getByRole("button", { name: "关闭设置面板" }).click()
  await expect(model).toHaveValue("sk-unsaved-draft")
  await expect.poll(() => page.evaluate(() => (window as typeof window & { __settingsConfirmCount: number }).__settingsConfirmCount)).toBe(3)
})
