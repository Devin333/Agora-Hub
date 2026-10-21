import { expect, test, type Page } from "./fixtures"

const widths = [320, 375, 414, 768, 1024, 1280, 1440]

test("Projects Lab remains usable without horizontal overflow across supported viewports", async ({ page, baseURL }, testInfo) => {
  await authenticate(page, baseURL)
  await mockProjectsLab(page)

  for (const width of widths) {
    await test.step(`${width}px`, async () => {
      await page.setViewportSize({ width, height: 900 })
      await page.goto("/projects/lab")

      await expect(page.getByRole("heading", { name: "Lab", exact: true })).toBeVisible()
      await expect(page.getByLabel("Projects Lab workspace")).toBeVisible()
      await expect(page.getByRole("heading", { name: "Start Lab Session" })).toBeVisible()
      if (width < 1280) {
        await expect(page.getByRole("button", { name: "Open Research navigation" })).toBeVisible()
      } else {
        await expect(page.getByRole("navigation", { name: "Portal modules" })).toBeVisible()
      }
      const overflowing = await page.locator("body *").evaluateAll((elements) =>
        elements
          .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
          .map((element) => ({
            tag: element.tagName,
            className: element.getAttribute("class"),
            right: Math.round(element.getBoundingClientRect().right),
          }))
      )
      expect(overflowing).toEqual([])

      await testInfo.attach(`projects-lab-${width}px`, {
        body: await page.screenshot({ fullPage: true }),
        contentType: "image/png",
      })
    })
  }
})

test("Projects Lab wraps long English and Chinese answers at mobile and desktop widths", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  await mockProjectsLab(page, { longText: true })

  for (const width of [320, 1440]) {
    await test.step(`${width}px long text`, async () => {
      await page.setViewportSize({ width, height: 900 })
      await page.goto("/projects/lab")
      const problem = "跨团队研究工作流需要可审计的证据链与发布门禁；This deliberately long brief checks wrapping without horizontal overflow."
      await page.getByPlaceholder("Describe the module or product problem").fill(problem)
      await page.getByRole("button", { name: "Start Session" }).click()
      await expect(page.getByText(/跨团队研究工作流需要可审计/)).toBeVisible()

      const overflowing = await page.locator("body *").evaluateAll((elements) =>
        elements
          .filter((element) => element.getBoundingClientRect().right > window.innerWidth + 1)
          .map((element) => element.tagName)
      )
      expect(overflowing).toEqual([])
    })
  }
})

async function authenticate(page: Page, baseURL: string | undefined) {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "newsroom-ui",
      JSON.stringify({
        state: { sidebarCollapsed: false, rightPanelOpen: true, theme: "light", locale: "en" },
        version: 0,
      })
    )
  })
  await page.route("**/api/auth/session", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        success: true,
        data: {
          session: {
            sessionId: "e2e-session",
            expiresAt: "2099-12-31T00:00:00Z",
            user: { userId: "e2e-user", username: "e2e", role: "admin" },
          },
        },
      }),
    })
  })
  await page.context().addCookies([{ name: "newsroom_session", value: "session-token", url: baseURL ?? "http://127.0.0.1:3000" }])
}

async function mockProjectsLab(page: Page, options: { longText?: boolean } = {}) {
  await page.route("**/api/v1/projects/lab/sessions", async (route) => {
    const request = route.request().postDataJSON() as { user_problem?: string }
    const longQuestion = options.longText
      ? "请说明在多团队协作、审计证据保留和离线部署限制下，哪个需求优先级最高；Which requirement must remain stable across every release?"
      : "Which requirement needs the most attention?"
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        success: true,
        data: {
          session: {
            id: "lab-session-e2e",
            user_problem: request.user_problem ?? "Lab brief",
            selected_case_ids: ["case-1"],
            current_stage: "clarifying_requirements",
            next_action: "answer_question",
            can_generate_solution: false,
            unanswered_question_ids: ["question-long"],
            questions: [{ id: "question-long", question: longQuestion, required: true }],
          },
        },
      }),
    })
  })
  await page.route("**/api/v1/projects/interactions", route =>
    route.fulfill({ json: { success: true, data: { recorded: true } } })
  )
  await page.route(/\/api\/v1\/projects(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        success: true,
        data: {
          hot: [{
            id: "project-1",
            slug: "project-1",
            name: "Research Workflow Toolkit",
            description: "A real Project Radar record used by the Lab viewport test.",
            project_type: "tool",
            tags: ["workflow"],
            source_confidence: 0.9,
            source_count: 3,
          }],
          rising: [],
          tools: [],
          cases: [
            { id: "case-1", project_id: "project-1", title: "Requirements workflow", business_domain: "research", module_type: "workflow" },
            { id: "case-2", project_id: "project-1", title: "Evidence workflow", business_domain: "research", module_type: "rag" },
            { id: "case-3", project_id: "project-1", title: "Review workflow", business_domain: "research", module_type: "evaluation" },
          ],
          collections: [],
          watchlist: [],
          recommendations: [],
          meta: { source: "artifact", source_run_id: "run-e2e", data_state: "ready", notices: [] },
          metrics: [],
        },
      }),
    })
  })
}
