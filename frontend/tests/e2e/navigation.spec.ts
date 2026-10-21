import { expect, test, type Page } from "./fixtures"

const routes = [
  "/",
  "/news",
  "/news/news-1-a",
  "/papers",
  "/papers/tasks",
  "/papers/methods",
  "/projects",
  "/projects/hot",
  "/projects/rising",
  "/projects/tools",
  "/projects/cases",
  "/projects/lab",
  "/projects/collections",
  "/projects/watchlist",
  "/topics",
  "/topics/agent-runtime-observability",
  "/tech",
  "/tech/papers",
  "/tech/repos",
  "/tech/frameworks",
  "/community",
  "/topics?view=evidence-graph",
  "/reports",
  "/reports/report-daily-ai-runtime",
  "/search?q=agent&type=topic",
  "/studio",
  "/studio/runs",
  "/studio/runs/run-daily-20260522-0800",
  "/studio/runs/not-found-run",
  "/studio/boards",
  "/studio/evidence",
  "/studio/quality",
  "/studio/review",
  "/studio/artifacts",
  "/studio/sources",
  "/admin",
]

for (const route of routes) {
  test(`${route} is reachable`, async ({ page, baseURL }) => {
    if (route !== "/") {
      await authenticate(page, baseURL)
    }

    const response = await page.goto(route)
    expect(response?.ok()).toBeTruthy()
    await expect(page.locator("body")).toBeVisible()
  })
}

for (const route of ["/papers/tasks/agents", "/papers/methods/react"]) {
  test(`${route} returns not found without real taxonomy evidence`, async ({ page, baseURL }) => {
    await authenticate(page, baseURL)

    const response = await page.goto(route)

    expect(response?.status()).toBe(404)
    await expect(page.getByRole("heading", { name: "This page could not be found." })).toBeVisible()
  })
}

test("/ renders the Portal homepage modules", async ({ page }) => {
  await setEnglishLocale(page)
  await page.goto("/")

  await expect(page.getByRole("heading", { name: /AI intelligence front page/i })).toBeVisible()
  const modules = page.getByRole("region", { name: "Portal modules" })
  await expect(modules.getByRole("link", { name: /View AI News/i })).toBeVisible()
  await expect(modules.getByRole("link", { name: /View Project Radar/i })).toHaveAttribute("href", "/projects")
  await expect(modules.getByRole("link", { name: /View Paper Radar/i })).toBeVisible()
  await expect(modules.getByRole("link", { name: /View Community Pulse/i })).toHaveAttribute("href", "/community")
  await expect(modules.getByRole("link", { name: /View Cross-board Evidence Graph/i })).toBeVisible()
  await expect(modules.getByRole("link", { name: /View Reports \/ Briefings/i })).toBeVisible()
  const researchEntries = page.getByRole("region", { name: "Paper Radar research entries" })
  await expect(researchEntries.getByRole("link", { name: /Trending Papers/i })).toHaveAttribute("href", "/papers")
  await expect(researchEntries.getByRole("link", { name: /Tasks/i })).toHaveAttribute("href", "/papers/tasks")
  await expect(researchEntries.getByRole("link", { name: /Methods/i })).toHaveAttribute("href", "/papers/methods")
  await expect(page.locator("body")).not.toContainText("Quality Gate")
})

test("Research header opens the Projects product navigation", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  await page.goto("/papers")

  const modules = page.getByRole("navigation", { name: "Portal modules" })
  await modules.getByRole("button", { name: "Projects" }).hover()

  await expect(modules.getByRole("link", { name: /^Projects\b/i })).toHaveAttribute("href", "/projects")
  await expect(modules.getByRole("link", { name: /^Hot Projects\b/i })).toHaveAttribute("href", "/projects/hot")
  await expect(modules.getByRole("link", { name: /^Rising Projects\b/i })).toHaveAttribute("href", "/projects/rising")
  await expect(modules.getByRole("link", { name: /^Tools\b/i })).toHaveAttribute("href", "/projects/tools")
  await modules.getByRole("link", { name: /^Projects\b/i }).click()

  await expect(page).toHaveURL(/\/projects$/)
})

test("/projects renders the Projects product home without API failure state", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  await page.goto("/projects")

  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible()
  await expect(page.getByText("Projects API v1")).toBeVisible()
  await expect(page.locator("body")).not.toContainText("Projects failed to load")
  await expect(page.locator("body")).not.toContainText("Internal Server Error")
})

test("/community renders the Community Pulse board", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  await mockCommunityPulse(page)

  await page.goto("/community")

  await expect(page.getByRole("heading", { name: /Community Pulse Board/i })).toBeVisible()
  await expect(page.getByText("Hot Discussion")).toBeVisible()
  await expect(page.getByText("Agent memory debate").first()).toBeVisible()
})

test("/ home Community Pulse card opens /community", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  await mockCommunityPulse(page)
  await page.goto("/")

  await page.getByRole("region", { name: "Portal modules" }).getByRole("link", { name: /View Community Pulse/i }).click()

  await expect(page).toHaveURL(/\/community$/)
  await expect(page.getByRole("heading", { name: /Community Pulse Board/i })).toBeVisible()
})

test("/news?source=community redirects to /community with mapped filters", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  await page.goto("/news?source=community&q=agent&topic=agents&sentiment=mixed&period=weekly&sort=controversial&limit=12")

  await expect(page).toHaveURL(/\/community\?q=agent&topic=agents&sentiment=mixed&period=weekly&sort=controversial&limit=12$/)
})

test("/papers uses one breadcrumb and real paper actions", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  await page.goto("/papers")

  await expect(page.getByRole("navigation", { name: "Research breadcrumb" })).toHaveCount(1)
  await expect(page.getByText("stars / hr")).toHaveCount(0)
  await expect(page.getByRole("button", { name: /^upvote$/i })).toHaveCount(0)
  await expect(page.getByRole("button", { name: /^collection$/i })).toHaveCount(0)
  await expect(page.getByRole("button", { name: /^reading list$/i })).toHaveCount(0)

  const githubHref = await page.locator("a[href^='https://github.com/']").first().getAttribute("href")
  expect(githubHref).toMatch(/^https:\/\/github\.com\/[^/]+\/[^/]+/)
  expect(githubHref).not.toBe("https://github.com/")

  const pdfHref = await page.locator("a[href*='/pdf/'], a[href$='.pdf']").first().getAttribute("href")
  expect(pdfHref).toMatch(/\/pdf\/|\.pdf($|\?)/)
})

test("/papers period tabs and drawer deep-link work", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  const papersLoaded = page.waitForResponse((response) => response.url().includes("/api/papers?") && response.request().method() === "GET")
  await page.goto("/papers")
  await papersLoaded

  const main = page.locator("main")
  await expect(main.getByRole("textbox", { name: /search papers|搜索论文/i })).toHaveCount(0)
  await main.locator("[aria-label='Paper period'] button").nth(1).click()
  await expect(page).toHaveURL(/period=weekly/)

  await page.goto("/papers?paper=arxiv-2605.22823")
  await expect(page.getByRole("dialog", { name: /paper detail/i })).toBeVisible()
  await expect(page.getByRole("heading", { name: "Agora Hub AI" })).toBeVisible()
  await expect(page.getByRole("heading", { name: "News and sources" })).toBeVisible()
  await expect(page.getByRole("heading", { name: "Community signals" })).toBeVisible()
  await expect(page.getByRole("heading", { name: "Evidence references" })).toBeVisible()
  await page.getByRole("button", { name: /dismiss|关闭/i }).click()
  await expect(page).toHaveURL(/\/papers$/)
})

test("/papers does not fabricate taxonomy links without real taxonomy evidence", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  const papersLoaded = page.waitForResponse((response) => response.url().includes("/api/papers?") && response.request().method() === "GET")
  await page.goto("/papers")
  await papersLoaded

  const sidebar = page.locator("main aside").last()
  await expect(sidebar.getByRole("heading", { name: "Methods" })).toBeVisible()
  await expect(sidebar.getByRole("heading", { name: "Tasks" })).toBeVisible()
  await expect(sidebar.locator("a[href^='/papers/tasks/']")).toHaveCount(0)
  await expect(sidebar.locator("a[href^='/papers/methods/']")).toHaveCount(0)
})

test("/tech/repos opens a Project Radar detail drawer from URL", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  await mockProjectRadar(page)

  await page.goto("/tech/repos?project=openai-codex")

  await expect(page.getByRole("heading", { name: /Project Radar Board/i })).toBeVisible()
  const drawer = page.getByRole("dialog", { name: /project detail/i })
  await expect(drawer).toBeVisible()
  await expect(drawer.getByRole("heading", { name: "codex" })).toBeVisible()
  await expect(drawer.getByRole("link", { name: /open repo/i })).toHaveAttribute("href", "https://github.com/openai/codex")

  await page.getByRole("button", { name: /close project detail/i }).click()
  await expect(page).toHaveURL(/\/tech\/repos$/)
})

test("/topics evidence graph renders the structured PRD-07 surface", async ({ page, baseURL }) => {
  await authenticate(page, baseURL)
  await page.goto("/topics?view=evidence-graph&topic=Agent")

  await expect(page.getByRole("heading", { name: "Cross-board Evidence Graph" })).toBeVisible()
  await expect(page.getByLabel("搜索主题")).toBeVisible()
  await expect(page.getByText("Graph Summary")).toBeVisible()
  await expect(page.getByText("Signal Mix")).toBeVisible()
  await expect(page.getByText("Evidence Chain")).toBeVisible()
  await expect(page.getByText("Timeline")).toBeVisible()
  await expect(page.getByText("Evidence Inspector")).toBeVisible()
  await expect(page.getByText("Related Reports")).toBeVisible()
})

async function authenticate(page: Page, baseURL: string | undefined) {
  await setEnglishLocale(page)
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
            user: {
              userId: "e2e-user",
              username: "e2e",
              role: "admin",
            },
          }
        }
      })
    })
  })
  await page.context().addCookies([
    {
      name: "newsroom_session",
      value: "session-token",
      url: baseURL ?? "http://127.0.0.1:3000"
    }
  ])
}

async function setEnglishLocale(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "newsroom-ui",
      JSON.stringify({
        state: {
          sidebarCollapsed: false,
          rightPanelOpen: true,
          theme: "light",
          locale: "en"
        },
        version: 0
      })
    )
  })
}

async function mockProjectRadar(page: Page) {
  const project = {
    id: "codex",
    slug: "openai-codex",
    name: "codex",
    fullName: "openai/codex",
    description: "A coding agent that runs in your terminal.",
    repoUrl: "https://github.com/openai/codex",
    owner: "openai",
    language: "TypeScript",
    stars: 12000,
    forks: 500,
    openIssues: 42,
    starGrowth7d: 320,
    scores: { trendScore: 88, activityScore: 60, evidenceScore: 2 },
    categoryRefs: [{ category: "agent_framework", label: "Agent Framework" }],
    categories: ["agent_framework"],
    tags: ["devtool"],
    topics: ["Agent Framework", "devtool"],
    maturity: "rising",
    relationCounts: { papers: 0, news: 1, community: 1 },
    relatedNews: [{ title: "Codex launch", url: "https://example.com/news", sourceName: "Example" }],
    relatedCommunityTopics: [{ title: "HN discussion", url: "https://news.ycombinator.com/item?id=1", sourceName: "Hacker News" }]
  }

  await page.route("**/api/projects/openai-codex", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        success: true,
        data: { project, dataState: "ready", source: "artifact", notices: [] },
        error: null
      })
    })
  })

  await page.route(/\/api\/projects(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        success: true,
        data: {
          items: [project],
          allItems: [project],
          allFiltered: [project],
          metrics: [
            { label: "Projects", value: 1 },
            { label: "With stars", value: 1 },
            { label: "Star delta", value: 320 },
            { label: "Active signals", value: 1, hint: "2 related references" }
          ],
          options: {
            categories: [{ value: "agent_framework", label: "Agent Framework", count: 1 }],
            sources: [{ value: "github", label: "GitHub", count: 1 }],
            languages: [{ value: "typescript", label: "TypeScript", count: 1 }],
            topics: [{ value: "Agent Framework", label: "Agent Framework", count: 1 }],
            maturity: [{ value: "rising", label: "Rising", count: 1 }]
          },
          page: { page: 1, pageSize: 24, total: 1, hasNext: false, nextCursor: null },
          dataState: "ready",
          source: "artifact",
          notices: []
        },
        error: null
      })
    })
  })
}

async function mockCommunityPulse(page: Page) {
  const signal = {
    id: "signal-1",
    slug: "agent-memory-debate",
    source: "hackernews",
    sourceName: "Hacker News",
    title: "Agent memory debate",
    url: "https://news.ycombinator.com/item?id=1",
    summary: "Developers discuss memory latency and reliability.",
    postedAt: "2026-05-25T00:00:00Z",
    collectedAt: "2026-05-25T00:00:00Z",
    score: 75,
    comments: 18,
    sentiment: "mixed",
    topics: ["agents"],
    entities: [],
    heatScore: 88,
    controversyScore: 42,
    adoptionScore: 35,
    relatedPaperIds: ["paper-1"],
    relatedProjectIds: ["project-1"],
    relatedNewsIds: ["news-1"],
    relatedPapers: [{ id: "paper-1", title: "Memory paper" }],
    relatedProjects: [{ id: "project-1", name: "Memory repo" }],
    relatedNews: [{ id: "news-1", title: "Memory launch" }],
    evidenceLinks: [
      {
        id: "evidence-1",
        sourceName: "Hacker News",
        sourceType: "hackernews",
        url: "https://news.ycombinator.com/item?id=1",
        excerpt: "Public excerpt only."
      }
    ]
  }
  const cluster = {
    id: "cluster-signal-1",
    title: "Agent memory debate",
    summary: "Developers discuss memory latency and reliability.",
    signalIds: ["signal-1"],
    topicIds: ["agents"],
    positiveArguments: [],
    negativeArguments: [],
    neutralFacts: ["Latency is the main blocker.", "Public excerpt only."],
    controversyScore: 42,
    lastUpdatedAt: "2026-05-25T00:00:00Z"
  }

  await page.route(/\/api\/community\/signals(?:\?.*)?$/, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        success: true,
        data: {
          items: [signal],
          allItems: [signal],
          allFiltered: [signal],
          clusters: [cluster],
          facets: {
            sources: [{ source: "hackernews", label: "Hacker News", count: 1 }],
            topics: [{ topic: "agents", label: "agents", count: 1 }],
            sentiments: [{ sentiment: "mixed", label: "Mixed", count: 1 }]
          },
          nextCursor: null,
          page: { items: [signal], total: 1, page: 1, pageSize: 10, hasNext: false, nextCursor: null },
          metrics: {
            totalSignals: 1,
            periodSignals: 1,
            activeSources: 1,
            hotSignals: 1,
            controversialSignals: 0,
            averageHeatScore: 88,
            averageControversyScore: 42,
            heatSummary: "Current lead: Agent memory debate"
          },
          dataState: "ready",
          source: "artifact",
          generatedAt: "2026-05-26T00:00:00Z",
          notices: []
        },
        error: null
      })
    })
  })

  await page.route("**/api/community/signals/signal-1", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        success: true,
        data: {
          signal,
          relatedPapers: signal.relatedPapers,
          relatedProjects: signal.relatedProjects,
          relatedNews: signal.relatedNews,
          evidenceLinks: signal.evidenceLinks,
          clusters: [cluster],
          notices: []
        },
        error: null
      })
    })
  })
}
