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
  await page.route("**/api/research/desktop/capabilities", route => route.fulfill({ json: { success: true, data: { generation, capabilities: [] } } }))
  await page.route("**/api/research/personal-memory/info", route => route.fulfill({ json: { info: { storage: "local_sqlite", status: "available", data_root: "C:/Agora", database_path: "C:/Agora/memory.db", remote_processing_default: false, remote_processing_auto_enabled: false } } }))
  await page.route("**/api/research/personal-memory/policy", route => route.fulfill({ json: { policy: { allow_remote_processing: false, revision: 1, created_at: "2026-10-14T00:00:00Z", updated_at: "2026-10-14T00:00:00Z" } } }))
}

async function expectDialogMonochrome(page: Page) {
  const violations = await page.getByRole("dialog", { name: "运行状态、数据边界与诊断" }).evaluate(dialog => {
    const elements = [dialog, ...dialog.querySelectorAll<HTMLElement>("*")]
    const failures: string[] = []
    for (const element of elements) {
      const style = getComputedStyle(element)
      const properties: Array<[string, string, boolean]> = [
        ["color", style.color, true],
        ["backgroundColor", style.backgroundColor, true],
        ["borderTopColor", style.borderTopColor, style.borderTopStyle !== "none" && style.borderTopWidth !== "0px"],
        ["borderRightColor", style.borderRightColor, style.borderRightStyle !== "none" && style.borderRightWidth !== "0px"],
        ["borderBottomColor", style.borderBottomColor, style.borderBottomStyle !== "none" && style.borderBottomWidth !== "0px"],
        ["borderLeftColor", style.borderLeftColor, style.borderLeftStyle !== "none" && style.borderLeftWidth !== "0px"],
        ["outlineColor", style.outlineColor, style.outlineStyle !== "none" && style.outlineWidth !== "0px"],
        ["textDecorationColor", style.textDecorationColor, style.textDecorationLine !== "none"],
        ["fill", style.fill, style.fill !== "none"],
        ["stroke", style.stroke, style.stroke !== "none"],
      ]
      for (const [property, value, visible] of properties) {
        if (!visible) continue
        const match = value.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)(?:\s*,\s*([\d.]+))?/)
        if (!match || match[4] === "0") continue
        if (match[1] !== match[2] || match[2] !== match[3]) failures.push(`${element.tagName.toLowerCase()}.${String(element.className)}: ${property}=${value}`)
      }
    }
    return failures
  })
  expect(violations).toEqual([])
}

test("settings modal is monochrome in light and dark themes and App Escape confirms dirty close once", async ({ page }) => {
  await installSettingsFixture(page)
  await page.goto("/paper-agent")

  await page.getByRole("button", { name: "设置与诊断" }).click()
  await expect(page.getByRole("dialog", { name: "运行状态、数据边界与诊断" })).toBeVisible()
  await expectDialogMonochrome(page)
  await page.getByRole("button", { name: "关闭设置面板" }).click()

  await page.getByRole("button", { name: "使用黑色主题" }).click()
  await page.getByRole("button", { name: "设置与诊断" }).click()
  await expectDialogMonochrome(page)

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
