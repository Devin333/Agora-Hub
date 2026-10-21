import { expect, test, type Page } from "./fixtures"

const existingStudioRoutes = [
  "/studio",
  "/studio/runs",
  "/studio/quality",
  "/studio/artifacts"
]

const futureStudioRoutes = [
  "/studio/boards",
  "/studio/evidence",
  "/studio/review"
]

async function switchStudioToEnglish(page: Page) {
  await page.getByRole("button", { name: "切换到 English" }).click()
  await expect(page.getByRole("navigation", { name: "Operations Studio" })).toBeVisible()
}

test.describe("Operations Studio acceptance smoke", () => {
  test.describe.configure({ mode: "serial" })

  for (const route of existingStudioRoutes) {
    test(`${route} renders inside the Studio shell`, async ({ page }) => {
      const response = await page.goto(route)

      expect(response?.ok()).toBeTruthy()
      await switchStudioToEnglish(page)
      await expect(page.locator("body")).toBeVisible()
      await expect(page.getByRole("navigation", { name: "Operations Studio" })).toBeVisible()
    })
  }

  test("/studio exposes current module entry points", async ({ page }) => {
    await page.goto("/studio")
    await switchStudioToEnglish(page)

    const entries = page.getByRole("navigation", { name: "Operations Studio" })
    await expect(entries.getByRole("link", { name: /Run Center/ })).toBeVisible()
    await expect(entries.getByRole("link", { name: /Artifact \/ Replay/ })).toBeVisible()
    await expect(entries.getByRole("link", { name: /Quality Gate/ })).toBeVisible()

    await entries.getByRole("link", { name: /Run Center/ }).click()
    await expect(page).toHaveURL(/\/studio\/runs$/)
  })

  test("/studio/runs shows the run list and links to failed run detail", async ({ page }) => {
    await page.goto("/studio/runs")
    await switchStudioToEnglish(page)

    await expect(page.getByRole("heading", { name: "Run Center" })).toBeVisible()
    await expect(page.locator("table").first()).toBeVisible()

    const runDetailLink = page.locator("table a[href^='/studio/runs/']").first()
    await expect(runDetailLink).toBeVisible()

    const href = await runDetailLink.getAttribute("href")
    expect(href).toMatch(/^\/studio\/runs\/[^/]+$/)

    await page.goto(href!)
    await expect(page).toHaveURL(/\/studio\/runs\/[^/]+$/)
  })

  test("/studio/runs failed run detail shows steps, events, and errors", async ({ page }) => {
    await page.goto("/studio/runs/run-daily-20260522-0800")
    await switchStudioToEnglish(page)

    await expect(page).toHaveURL(/\/studio\/runs\/run-daily-20260522-0800$/)
    await expect(page.locator("body")).toContainText("step-quality")
    await expect(page.locator("body")).toContainText(/fallback|Run Runtime API/i)

    await page.getByRole("button", { name: /^Errors$/i }).click()
    await expect(page.locator("body")).toContainText(/citation|quality|step-quality|error|引用|错误/i)
  })

  test("/studio/quality shows failed, warning, and passed quality states", async ({ page }) => {
    await page.goto("/studio/quality")
    await switchStudioToEnglish(page)

    await expect(page.getByRole("heading", { name: "Quality Gate" })).toBeVisible()
    await expect(page.locator("body")).toContainText(/Passed/i)
    await expect(page.locator("body")).toContainText(/Warning/i)
    await expect(page.locator("body")).toContainText(/Failed/i)
    await expect(page.locator("body")).toContainText(/Review required/i)
    await expect(page.locator("a[href^='/studio/quality/reports/']").first()).toBeVisible()
  })

  test("/studio/artifacts shows artifact list and preview metadata", async ({ page }) => {
    await page.goto("/studio/artifacts")
    await switchStudioToEnglish(page)

    await expect(page.locator("body")).toContainText(/manifest/i)
    await expect(page.locator("body")).toContainText(/events/i)
    await expect(page.locator("body")).toContainText(/step results/i)
    await expect(page.locator("table").first()).toBeVisible()
    await expect(page.locator("a[href*='/studio/artifacts/runs/']").first()).toBeVisible()
    await expect(page.locator("a[href$='/replay']").first()).toBeVisible()
  })
})

test.describe("Operations Studio future module acceptance", () => {
  test.describe.configure({ mode: "serial" })

  for (const route of futureStudioRoutes) {
    test.fixme(`${route} acceptance remains pending until its module task lands`, async ({ page }) => {
      await page.goto(route)
      await expect(page.locator("body")).toBeVisible()
    })
  }

  test.fixme("Board Center shows five boards and cross_board insight area", async ({ page }) => {
    await page.goto("/studio/boards")
    await expect(page.getByText("AI News")).toBeVisible()
    await expect(page.getByText("Project Radar")).toBeVisible()
    await expect(page.getByText("Paper Radar")).toBeVisible()
    await expect(page.getByText("Community Pulse")).toBeVisible()
    await expect(page.getByText("Cross Board")).toBeVisible()
    await page.getByRole("link", { name: /cross board/i }).click()
    await expect(page.locator("body")).toContainText(/cross_board|cross board insight/i)
  })

  test.fixme("Evidence Center shows unsupported claims and run claim-support table", async ({ page }) => {
    await page.goto("/studio/evidence")
    await expect(page.locator("body")).toContainText(/unsupported claims/i)
    await page.getByRole("link", { name: /run-daily-20260522-0800/i }).click()
    await expect(page.locator("body")).toContainText(/claim-support|claim support/i)
  })

  test.fixme("Human Review validates approve, reject, and modify required fields", async ({ page }) => {
    await page.goto("/studio/review")
    await expect(page.locator("body")).toContainText(/pending review queue/i)
    await page.getByRole("button", { name: /approve/i }).click()
    await expect(page.locator("body")).toContainText(/required|decided_by/i)
    await page.getByRole("button", { name: /reject/i }).click()
    await expect(page.locator("body")).toContainText(/required|reason/i)
    await page.getByRole("button", { name: /modify/i }).click()
    await expect(page.locator("body")).toContainText(/required|modifications/i)
  })
})
