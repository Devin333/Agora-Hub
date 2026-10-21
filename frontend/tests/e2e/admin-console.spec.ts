import { expect, test } from "./fixtures"

test("admin console supports bilingual navigation and core selections", async ({ page }) => {
  await page.goto("/admin")

  await expect(page).toHaveURL(/\/studio$/)
  await expect(page.getByRole("heading", { name: "运行时仪表盘" })).toBeVisible()
  await expect(page.getByText("活跃运行", { exact: true })).toBeVisible()
  await expect(page.getByRole("navigation", { name: "运维工作台" })).toBeVisible()

  await page.getByRole("button", { name: "切换到 English" }).click()
  await expect(page.getByRole("heading", { name: "Runtime Dashboard" })).toBeVisible()
  await expect(page.getByText("Active runs", { exact: true })).toBeVisible()

  const operationsNavigation = page.getByRole("navigation", { name: "Operations Studio" })
  await expect(operationsNavigation).toBeVisible()

  await operationsNavigation.getByRole("link", { name: /Run Center/ }).click()
  await expect(page).toHaveURL(/\/studio\/runs$/)
  await expect(page.getByRole("heading", { name: "Run Center" })).toBeVisible()
  await expect(page.locator("table").first()).toBeVisible()

  await operationsNavigation.getByRole("link", { name: /Quality Gate/ }).click()
  await expect(page).toHaveURL(/\/studio\/quality$/)
  await expect(page.getByRole("heading", { name: "Quality Gate" })).toBeVisible()

  await operationsNavigation.getByRole("link", { name: /Sources/ }).click()
  await expect(page).toHaveURL(/\/studio\/sources$/)
  await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible()
})
