import { expect, test as base, type Locator, type Page, type Route } from "@playwright/test"

export const test = base.extend({
  page: async ({ page, baseURL }, providePage) => {
    await page.context().addCookies([{
      name: "newsroom_session",
      value: "e2e-session-token",
      url: baseURL ?? "http://127.0.0.1:3000",
    }])
    await page.route("**/api/auth/session", route => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ success: true, data: { initialized: true, session: null } }),
    }))
    await page.route("**/api/research/history", route => route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ success: true, data: { visits: [], groups: [], revision: 0 } }),
    }))
    await providePage(page)
  },
})

export { expect }
export type { Locator, Page, Route }
