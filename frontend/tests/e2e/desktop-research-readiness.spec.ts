import { expect, test, type Locator, type Page, type Route } from "@playwright/test"

const fingerprint = `sha256:${"a".repeat(64)}`
const generation = "gen_0123456789abcdef0123456789abcdef"

interface DesktopFixtureState {
  owner: string
  analyzePosts: number
  operationGets: number
  runReady: boolean
  revision: number
  workspace: Record<string, unknown>
}

test.describe("local desktop research regression", () => {
  test.skip(
    process.env.NEWSROOM_DEPLOYMENT_PROFILE !== "local-desktop",
    "requires the local-desktop profile",
  )

  test("migrates legacy reader URLs and preserves query location", async ({ page, baseURL }) => {
    const state = fixtureState()
    await installDesktopFixture(page, baseURL, state)
    await page.goto(
      `/design-demo/papers/2601.01234/read?resumeSection=methods&resumePage=3&returnTo=${encodeURIComponent("/design-demo/papers?q=Agent")}`,
    )
    await expect(page).toHaveURL(
      /\/papers\/2601\.01234\/read\?resumeSection=methods&resumePage=3&returnTo=%2Fpapers%3Fq%3DAgent/,
    )
    await expect(page.getByRole("heading", { name: "Owner alice paper" })).toBeVisible()
  })

  test("recovers a lost first receipt by querying the durable identity without resubmitting", async ({
    page,
    baseURL,
  }) => {
    const state = fixtureState()
    await installDesktopFixture(page, baseURL, state)
    await page.goto("/papers/2601.01234/read")
    await expect(page.getByRole("heading", { name: "Owner alice paper" })).toBeVisible()

    await page.getByRole("button", { name: "开始真实分析" }).click()
    await expect(page.getByText("请求结果未知，需要按原身份查询")).toBeVisible()
    expect(state.analyzePosts).toBe(1)

    await page.getByRole("button", { name: "检查已有请求" }).click()
    await expect(page.getByRole("link", { name: "查看本次运行报告" })).toBeVisible()
    expect(state.analyzePosts).toBe(1)
    expect(state.operationGets).toBe(1)
  })

  test("clears owner-scoped reader data after account change", async ({ page, baseURL }) => {
    const state = fixtureState()
    await installDesktopFixture(page, baseURL, state)
    await page.goto("/papers/2601.01234/read")
    await expect(page.getByRole("heading", { name: "Owner alice paper" })).toBeVisible()

    state.owner = "bob"
    state.workspace = { visits: [], groups: [] }
    state.revision = 0
    await page.evaluate(() => {
      window.dispatchEvent(new Event("newsroom:portal-session-changed"))
    })
    await expect(page.getByText("正在核对账号与研究工作区…")).toBeVisible()
    await expect(page.getByRole("heading", { name: "Owner bob paper" })).toBeVisible()
    await expect(page.getByRole("heading", { name: "Owner alice paper" })).toHaveCount(0)
  })

  test("keeps primary desktop reader actions visible and keyboard reachable", async ({
    page,
    baseURL,
  }) => {
    const state = fixtureState()
    await installDesktopFixture(page, baseURL, state)
    const viewports = [
      { name: "1366x768", width: 1366, height: 768 },
      { name: "1920x1080", width: 1920, height: 1080 },
      {
        name: "1366x768 at 125% effective CSS viewport",
        width: Math.floor(1366 / 1.25),
        height: Math.floor(768 / 1.25),
      },
    ]

    for (const viewport of viewports) {
      await test.step(viewport.name, async () => {
        await page.setViewportSize({ width: viewport.width, height: viewport.height })
        await page.goto("/papers/2601.01234/read")
        const analyze = page.getByRole("button", { name: "开始真实分析" })
        const notes = page.getByRole("button", { name: "阅读笔记" })
        await expect(analyze).toBeVisible()
        await expect(notes).toBeVisible()
        await expectControlUnobscured(page, analyze)
        await expectControlUnobscured(page, notes)
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

        await page.locator("body").focus()
        await focusByTab(page, analyze)
        await focusByTab(page, notes)
        await page.keyboard.press("Enter")
        const noteInput = page.getByLabel("我的阅读笔记")
        await expect(noteInput).toBeVisible()
        await expectControlUnobscured(page, noteInput)
        await noteInput.focus()
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.keyboard.type("键盘输入可达")
        await expect(noteInput).toHaveValue("键盘输入可达")

        await page.keyboard.press("Control+K")
        await expect(page.getByRole("combobox", { name: "搜索研究、资料或模块" })).toBeFocused()
        await page.keyboard.press("Escape")
        await expect(page.getByRole("dialog", { name: "全局研究搜索" })).toHaveCount(0)
        await expect(noteInput).toBeFocused()
      })
    }
  })
})

function fixtureState(): DesktopFixtureState {
  return {
    owner: "alice",
    analyzePosts: 0,
    operationGets: 0,
    runReady: false,
    revision: 0,
    workspace: { visits: [], groups: [] },
  }
}

async function installDesktopFixture(
  page: Page,
  baseURL: string | undefined,
  state: DesktopFixtureState,
) {
  await page.context().addCookies([
    {
      name: "newsroom_session",
      value: "desktop-e2e-session",
      url: baseURL ?? "http://127.0.0.1:3000",
    },
  ])
  await page.route("**/api/auth/session", route =>
    route.fulfill({
      json: {
        success: true,
        data: {
          local_desktop: {
            deployment_profile: "local-desktop",
            instance_key: `local-desktop:${baseURL ?? "http://127.0.0.1:3000"}`,
            release_id: "desktop-e2e-release",
            config_digest: "a".repeat(64),
            boot_id: "b".repeat(32),
          },
          session: {
            sessionId: `session-${state.owner}`,
            expiresAt: "2099-12-31T00:00:00Z",
            user: {
              userId: state.owner,
              username: state.owner,
              role: "user",
            },
          },
        },
      },
    }),
  )
  await page.route("**/api/research/history", async route => {
    if (route.request().method() === "PUT") {
      const body = JSON.parse(route.request().postData() ?? "{}")
      const { revision: _revision, ...workspace } = body
      state.workspace = workspace
      state.revision += 1
    }
    await route.fulfill({
      json: {
        success: true,
        data: { ...state.workspace, revision: state.revision },
      },
    })
  })
  await page.route("**/api/research/desktop/identity", route =>
    route.fulfill({
      json: {
        success: true,
        data: {
          owner: {
            userId: state.owner,
            tenantId: null,
            memoryNamespace: `research:user:${state.owner}`,
          },
          generation,
          requestContract: {
            fingerprint: "sha256-canonical-json-v1",
            requestId: "stable owner-scoped UUID",
            notFoundRetry: "query the same request identity",
          },
        },
      },
    }),
  )
  await page.route("**/api/research/desktop/papers/2601.01234/reader", route =>
    route.fulfill({ json: { success: true, data: readerPayload(state) } }),
  )
  await page.route("**/api/research/desktop/operations/analyze", async route => {
    state.analyzePosts += 1
    await route.abort("connectionfailed")
  })
  await page.route(/\/api\/research\/desktop\/operations\/[^?]+\?generation=/, async route => {
    state.operationGets += 1
    state.runReady = true
    const requestId = route.request().url().split("/operations/")[1].split("?")[0]
    await route.fulfill({
      json: {
        success: true,
        data: operationReceipt(
          durableOperation(state, decodeURIComponent(requestId)),
        ),
      },
    })
  })
}

function readerPayload(state: DesktopFixtureState) {
  const paper = {
    id: "2601.01234",
    slug: "2601.01234",
    title: `Owner ${state.owner} paper`,
    abstract: "Private owner-scoped paper.",
    authors: ["Researcher"],
    publishedAt: "2026-01-12",
    topics: ["research"],
    taskRefs: [],
    methodRefs: [],
    paperUrl: "https://arxiv.org/abs/2601.01234",
    pdfUrl: "https://arxiv.org/pdf/2601.01234",
    isPublished: false,
  }
  return {
    paper,
    reader: {
      paper,
      sections: [
        {
          id: "methods",
          paperId: paper.id,
          title: "Methods",
          level: 1,
          pageStart: 3,
          textExcerpt: "Owner-scoped verified methods section.",
          summary: "Methods summary",
          sectionType: "method",
        },
      ],
      aiSummary: null,
      readerNotes: [],
      relatedPapers: [],
      relatedProjects: [],
      relatedNews: [],
      quality: {
        paperId: paper.id,
        pdfAvailable: true,
        textExtracted: true,
        summaryAvailable: false,
        implementationVerified: false,
        benchmarkVerified: false,
        evidenceCoverage: 1,
      },
    },
    state: "ready",
    reasonCode: null,
    aiReady: state.runReady,
    sourceRevision: "revision-1",
    sourceFingerprint: fingerprint,
    ...(state.runReady ? { runId: "run-accepted-1" } : {}),
    capabilities: { analyze: true, ask: state.runReady },
  }
}

function durableOperation(state: DesktopFixtureState, requestId: string) {
  const operations = Array.isArray(state.workspace.pendingOperations)
    ? state.workspace.pendingOperations
    : []
  const operation = operations.find(item =>
    item &&
    typeof item === "object" &&
    "requestId" in item &&
    item.requestId === requestId,
  )
  if (!operation || typeof operation !== "object") {
    throw new Error(`durable operation not found: ${requestId}`)
  }
  const candidate = operation as Record<string, unknown>
  if (
    typeof candidate.generation !== "string" ||
    typeof candidate.fingerprint !== "string" ||
    candidate.kind !== "analyze"
  ) {
    throw new Error("durable operation identity is incomplete")
  }
  return {
    requestId,
    generation: candidate.generation,
    fingerprint: candidate.fingerprint,
    kind: candidate.kind,
  }
}

function operationReceipt(identity: ReturnType<typeof durableOperation>) {
  return {
    ...identity,
    status: "passed",
    updatedAt: "2026-10-12T00:00:00Z",
    paperId: "2601.01234",
    sourceRevision: "revision-1",
    runId: "run-accepted-1",
    recoverable: true,
    automaticRetryAllowed: false,
    result: {
      runId: "run-accepted-1",
      paperId: "2601.01234",
      sourceRevision: "revision-1",
      accepted: true,
      qualityPassed: true,
      eligibleForAcceptedExport: true,
      artifactStatus: "valid",
    },
  }
}


async function focusByTab(page: Page, target: Locator) {
  for (let index = 0; index < 40; index += 1) {
    await page.keyboard.press("Tab")
    if (await target.evaluate(element => element === document.activeElement)) return
  }
  throw new Error("keyboard traversal did not reach the expected reader control")
}

async function expectControlUnobscured(
  page: Page,
  target: Locator,
) {
  const observation = await target.evaluate(element => {
    const bounds = element.getBoundingClientRect()
    const fullyInsideViewport =
      bounds.width > 0 &&
      bounds.height > 0 &&
      bounds.left >= 0 &&
      bounds.top >= 0 &&
      bounds.right <= window.innerWidth &&
      bounds.bottom <= window.innerHeight
    const hit = document.elementFromPoint(
      bounds.left + bounds.width / 2,
      bounds.top + bounds.height / 2,
    )
    return {
      unobscured:
        fullyInsideViewport &&
        (hit === element || Boolean(hit && element.contains(hit))),
      bounds: {
        left: bounds.left,
        top: bounds.top,
        right: bounds.right,
        bottom: bounds.bottom,
        width: bounds.width,
        height: bounds.height,
      },
      viewport: { width: window.innerWidth, height: window.innerHeight },
      hit: hit
        ? {
            tagName: hit.tagName,
            className: typeof hit.className === "string" ? hit.className : "",
            text: hit.textContent?.trim().slice(0, 80) ?? "",
          }
        : null,
    }
  })
  expect(observation.unobscured, JSON.stringify(observation)).toBe(true)
}
