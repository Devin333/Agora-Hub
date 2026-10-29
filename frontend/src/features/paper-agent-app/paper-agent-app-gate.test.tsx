import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { LoginForm } from "@/app/login/login-form"
import { bootstrapAccount, fetchAuthSession, login } from "@/lib/auth/api"
import type { AuthSession } from "@/lib/papers/types"
import { historyState, selectHistoryOwner } from "@/lib/research/history"
import { startHistorySync } from "@/lib/research/history-sync"
import { PaperAgentApp } from "./paper-agent-app"

const navigation = vi.hoisted(() => ({
  refresh: vi.fn(),
  replace: vi.fn(),
  searchParams: new URLSearchParams()
}))

const mocks = vi.hoisted(() => ({
  account: {
    resolved: true,
    session: {
      user: { userId: "user-1", username: "admin", role: "admin" },
      sessionId: "session-1",
      expiresAt: "2026-12-31T00:00:00Z"
    } as AuthSession | null,
    pending: false,
    error: null as string | null,
    openLogin: vi.fn(),
    signOut: vi.fn()
  },
  workspace: {
    owner: "user-1" as string | null | undefined,
    status: "error" as "local" | "loading" | "saving" | "synced" | "error" | "conflict",
    message: "账号历史暂时无法同步。",
    revision: 0,
    generation: 1,
    dirty: false,
    ready: false,
    workspace: { visits: [], groups: [] }
  },
  guided: { active: null, busy: false, saveError: "", start: vi.fn() }
}))

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    refresh: navigation.refresh,
    replace: navigation.replace
  }),
  useSearchParams: () => navigation.searchParams
}))

vi.mock("@/lib/auth/api", () => ({
  bootstrapAccount: vi.fn(),
  fetchAuthSession: vi.fn(),
  login: vi.fn()
}))

vi.mock("@/components/auth/portal-account-provider", () => ({
  usePortalAccount: () => mocks.account
}))

vi.mock("@/lib/research/use-research-history", () => ({
  useOwnedResearchWorkspace: () => mocks.workspace
}))

vi.mock("@/lib/research/use-guided-research", () => ({
  useGuidedResearch: () => mocks.guided
}))

vi.mock("@/lib/research/desktop/client", () => ({
  fetchDesktopCapabilities: vi.fn(() => new Promise(() => {})),
  fetchDesktopIdentity: vi.fn(() => new Promise(() => {}))
}))

vi.mock("./desktop-lifecycle", () => ({
  useDesktopLifecycle: vi.fn()
}))

vi.mock("@/features/portal/components/research-source-entry", () => ({
  ResearchSourceEntry: () => null
}))

vi.mock("./personal-memory-panel", () => ({
  PersonalMemoryPanel: () => null
}))

vi.mock("./paper-agent-settings-panel", () => ({
  PaperAgentSettingsModal: () => null
}))

vi.mock("./paper-reader-pane", () => ({
  PaperReaderPane: () => null
}))

const session: AuthSession = {
  user: { userId: "user-1", username: "admin", role: "admin" },
  sessionId: "session-1",
  expiresAt: "2026-12-31T00:00:00Z"
}

function historyResponse(status: number, data?: { visits: never[]; groups: never[]; revision: number }) {
  return new Response(JSON.stringify(status === 200 ? { success: true, data } : { success: false, error: { code: "history_unavailable", message: "history unavailable" } }), {
    status,
    headers: { "Content-Type": "application/json" }
  })
}

function setProfile(profile: "default" | "hosted") {
  vi.stubEnv("NEWSROOM_DEPLOYMENT_PROFILE", profile)
  vi.stubEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", profile)
  if (profile === "hosted") {
    vi.stubEnv("NEWSROOM_PUBLIC_ORIGIN", "http://127.0.0.1:3120")
    vi.stubEnv("NEWSROOM_ALLOW_INSECURE_ORIGIN", "1")
  }
}

function expectGuestGate() {
  const trigger = screen.getByRole("button", { name: "登录并打开论文库" })
  expect(screen.getByText(/账号由管理员创建；没有账号请联系管理员/)).toBeInTheDocument()
  expect(screen.queryByRole("button", { name: /创建/ })).not.toBeInTheDocument()
  return trigger
}

function continueToLoginForm(nextPath: string) {
  navigation.searchParams = new URLSearchParams({ next: nextPath })
  cleanup()
  render(<LoginForm defaultNextPath="/" />)
}

describe("PaperAgentApp account and workspace gates", () => {
  beforeEach(() => {
    setProfile("default")
    mocks.account.resolved = true
    mocks.account.session = session
    mocks.account.error = null
    mocks.account.openLogin.mockReset()
    mocks.workspace.owner = "user-1"
    mocks.workspace.status = "error"
    mocks.workspace.message = "账号历史暂时无法同步。"
    mocks.workspace.ready = false
    mocks.workspace.dirty = false
    mocks.guided.busy = false
    navigation.refresh.mockReset()
    navigation.replace.mockReset()
    navigation.searchParams = new URLSearchParams()
    vi.mocked(bootstrapAccount).mockReset()
    vi.mocked(fetchAuthSession).mockReset()
    vi.mocked(login).mockReset()
  })

  afterEach(() => {
    selectHistoryOwner(null)
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  it("sends a hosted guest whose cookie went stale to login only, even before an administrator exists", async () => {
    setProfile("hosted")
    mocks.account.session = null
    mocks.workspace.owner = null
    mocks.workspace.message = ""
    render(<PaperAgentApp />)
    expectGuestGate()
    vi.mocked(fetchAuthSession).mockResolvedValue({ initialized: false, bootstrapAvailable: false, session: null })
    vi.mocked(login).mockResolvedValue(session)

    continueToLoginForm("/paper-agent")

    expect(await screen.findByRole("heading", { name: "登录 Agora Hub" })).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "创建第一个 Agora Hub 账号" })).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "admin" } })
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "correct horse" } })
    fireEvent.click(screen.getByRole("button", { name: "登录" }))

    await waitFor(() => expect(login).toHaveBeenCalledWith("admin", "correct horse"))
    expect(navigation.replace).toHaveBeenCalledWith("/paper-agent")
    expect(navigation.refresh).toHaveBeenCalled()
    expect(bootstrapAccount).not.toHaveBeenCalled()
  })

  it("returns a hosted session that ends while mounted to the account gate without leaking the workspace", () => {
    setProfile("hosted")
    mocks.workspace.status = "synced"
    mocks.workspace.message = ""
    mocks.workspace.ready = true
    const view = render(<PaperAgentApp />)
    expect(screen.getByTestId("paper-agent-app")).toBeInTheDocument()

    mocks.account.session = null
    mocks.workspace.owner = null
    view.rerender(<PaperAgentApp />)

    expect(screen.queryByTestId("paper-agent-app")).not.toBeInTheDocument()
    const trigger = expectGuestGate()
    fireEvent.click(trigger)
    expect(mocks.account.openLogin).toHaveBeenCalledWith(trigger)
  })

  it("opens the portal login dialog from the default-profile gate", () => {
    mocks.account.session = null
    mocks.workspace.owner = null
    mocks.workspace.message = ""

    render(<PaperAgentApp />)

    const trigger = expectGuestGate()
    fireEvent.click(trigger)
    expect(mocks.account.openLogin).toHaveBeenCalledOnce()
    expect(mocks.account.openLogin).toHaveBeenCalledWith(trigger)
  })

  it("shows the real initial history failure and retries the active owner sync path", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(historyResponse(500))
      .mockResolvedValueOnce(historyResponse(200, { visits: [], groups: [], revision: 0 }))
    vi.stubGlobal("fetch", fetcher)
    const stop = startHistorySync("user-1")

    try {
      await waitFor(() => expect(historyState()).toMatchObject({ owner: "user-1", ready: false, status: "error" }))
      render(<PaperAgentApp />)

      expect(screen.getByRole("heading", { name: "论文库暂时无法读取" })).toBeInTheDocument()
      expect(screen.getByRole("alert")).toHaveTextContent("账号历史暂时无法同步。")
      expect(screen.queryByText("正在核对账号与论文库")).not.toBeInTheDocument()
      expect(screen.queryByTestId("paper-agent-app")).not.toBeInTheDocument()

      fireEvent.click(screen.getByRole("button", { name: "重试读取论文库" }))

      await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2))
      await waitFor(() => expect(historyState()).toMatchObject({ owner: "user-1", ready: true, status: "synced" }))
    } finally {
      stop()
    }
  })

  it("does not expose another owner's failure or retry action during an owner transition", () => {
    mocks.workspace.owner = "other-user"

    render(<PaperAgentApp />)

    expect(screen.getByRole("status")).toHaveTextContent("正在核对账号与论文库")
    expect(screen.queryByRole("button", { name: "重试读取论文库" })).not.toBeInTheDocument()
    expect(screen.queryByText("账号历史暂时无法同步。")).not.toBeInTheDocument()
  })
})





