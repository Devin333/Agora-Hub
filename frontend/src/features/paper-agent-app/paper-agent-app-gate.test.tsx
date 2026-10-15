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

function setProfile(profile: "default" | "local-desktop") {
  vi.stubEnv("NEWSROOM_DEPLOYMENT_PROFILE", profile)
  vi.stubEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", profile)
}


function installDataClearBridge() {
  const clearData = {
    status: vi.fn().mockResolvedValue({ ok: false, state: "FAILED_PARTIAL", code: "data_clear_failed_partial", message: "需要重新确认" }),
    preview: vi.fn().mockResolvedValue({ ok: true, state: "CONFIRMATION_REQUIRED", code: "data_clear_preview_ready", message: "ready", previewId: "preauth-preview", expiresAt: "2099-10-15T08:00:00.000Z", stopped: true, scope: { profilePath: "C:/Users/test/AppData/Roaming/Agora", fileCount: 7, totalBytes: 2048, includes: ["账号与会话", "论文资料、笔记与个人记忆"], preserves: ["安装资源", "外部备份与导出", "其他 profile"], credentialPolicy: "只删除当前 profile 的专属凭据槽位。" } }),
    apply: vi.fn().mockResolvedValue({ ok: true, state: "PREPARED", code: "data_clear_prepared", message: "prepared", operationId: "operation-preauth", exitRequired: true }),
  }
  Object.assign(window, { agoraDesktop: { version: 1, runtime: { clearData } } })
  return clearData
}
function expectLocalDesktopGuestGate() {
  const entry = screen.getByRole("link", { name: "创建或登录本机账号" })
  expect(entry).toHaveAttribute("href", "/login?next=/paper-agent")
  expect(screen.getByText(/首次使用请创建本机账号；已有账号请直接登录/)).toBeInTheDocument()
  expect(screen.getByRole("button", { name: "管理/清除本机数据" })).toBeInTheDocument()
  expect(mocks.account.openLogin).not.toHaveBeenCalled()
  return entry
}

function renderStaleCookieGuestGate() {
  setProfile("local-desktop")
  mocks.account.session = null
  mocks.workspace.owner = null
  mocks.workspace.message = ""
  render(<PaperAgentApp />)
  return expectLocalDesktopGuestGate()
}

function renderGateAfterMountedSessionLoss() {
  setProfile("local-desktop")
  mocks.workspace.status = "synced"
  mocks.workspace.message = ""
  mocks.workspace.ready = true
  const view = render(<PaperAgentApp />)
  expect(screen.getByTestId("paper-agent-app")).toBeInTheDocument()

  mocks.account.session = null
  mocks.workspace.owner = null
  view.rerender(<PaperAgentApp />)
  return expectLocalDesktopGuestGate()
}

function continueToLoginForm(entry: HTMLElement) {
  const target = new URL(entry.getAttribute("href")!, "http://localhost")
  navigation.searchParams = target.searchParams
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
    delete (window as unknown as { agoraDesktop?: unknown }).agoraDesktop
  })

  it("routes a stale-cookie local desktop guest through bootstrap when the account store is uninitialized", async () => {
    const entry = renderStaleCookieGuestGate()
    vi.mocked(fetchAuthSession).mockResolvedValue({ initialized: false, session: null })
    vi.mocked(bootstrapAccount).mockResolvedValue(session)

    continueToLoginForm(entry)

    expect(await screen.findByRole("heading", { name: "创建第一个 Agora Hub 账号" })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "admin" } })
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "correct horse" } })
    fireEvent.click(screen.getByRole("button", { name: "创建账号" }))

    await waitFor(() => expect(bootstrapAccount).toHaveBeenCalledWith("admin", "correct horse"))
    expect(navigation.replace).toHaveBeenCalledWith("/paper-agent")
    expect(navigation.refresh).toHaveBeenCalled()
    expect(login).not.toHaveBeenCalled()
  })

  it("routes a mounted local desktop session that becomes guest through login and back to paper agent", async () => {
    const entry = renderGateAfterMountedSessionLoss()
    vi.mocked(fetchAuthSession).mockResolvedValue({ initialized: true, session: null })
    vi.mocked(login).mockResolvedValue(session)

    continueToLoginForm(entry)

    expect(await screen.findByRole("heading", { name: "登录 Agora Hub" })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "admin" } })
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "correct horse" } })
    fireEvent.click(screen.getByRole("button", { name: "登录" }))

    await waitFor(() => expect(login).toHaveBeenCalledWith("admin", "correct horse"))
    expect(navigation.replace).toHaveBeenCalledWith("/paper-agent")
    expect(navigation.refresh).toHaveBeenCalled()
    expect(bootstrapAccount).not.toHaveBeenCalled()
  })


  it("opens the real owned-profile clear flow before login without exposing private workspace", async () => {
    const clearData = installDataClearBridge()
    renderStaleCookieGuestGate()

    fireEvent.click(screen.getByRole("button", { name: "管理/清除本机数据" }))
    const dialog = await screen.findByRole("dialog", { name: "管理/清除本机数据" })
    expect(dialog).toHaveTextContent("不会读取账号论文库、笔记或个人记忆内容")
    expect(dialog).toHaveTextContent("FAILED_PARTIAL")
    expect(dialog).toHaveTextContent("不会自动继续删除")
    expect(screen.queryByText("Research 运行配置")).not.toBeInTheDocument()
    expect(screen.queryByText("个人记忆远程处理")).not.toBeInTheDocument()

    await waitFor(() => expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeEnabled())
    fireEvent.click(screen.getByRole("button", { name: "安全停止并预览清除范围" }))
    expect(await screen.findByText("账号与会话")).toBeInTheDocument()
    fireEvent.change(screen.getByRole("textbox", { name: "精确确认文本" }), { target: { value: "删除本机全部 Agora 数据" } })
    fireEvent.click(screen.getByRole("button", { name: "提交并等待第二次原生确认" }))

    expect(await screen.findAllByText("已受理、退出后清理，尚未完成。")).not.toHaveLength(0)
    expect(clearData.apply).toHaveBeenCalledWith({ previewId: "preauth-preview", confirmationText: "删除本机全部 Agora 数据" })
    expect(screen.queryByText("本机数据已清除")).not.toBeInTheDocument()
  })


  it("protects pre-auth clear preview when the mounted renderer still has drafts or active work", async () => {
    installDataClearBridge()
    mocks.workspace.dirty = true
    mocks.guided.busy = true
    renderStaleCookieGuestGate()

    fireEvent.click(screen.getByRole("button", { name: "管理/清除本机数据" }))
    expect(await screen.findByText(/存在未应用设置或其他未保存草稿/)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "安全停止并预览清除范围" })).toBeDisabled()
  })
  it("keeps the local clear entry visibly unavailable when the typed bridge is missing", () => {
    renderStaleCookieGuestGate()
    fireEvent.click(screen.getByRole("button", { name: "管理/清除本机数据" }))
    expect(screen.getByText("本机数据清除不可用")).toBeInTheDocument()
    expect(screen.getByText(/不会通过浏览器代替本机清除，也不会模拟清除成功/)).toBeInTheDocument()
  })
  it("keeps the default profile on the existing portal login dialog path", () => {
    mocks.account.session = null
    mocks.workspace.owner = null
    mocks.workspace.message = ""

    render(<PaperAgentApp />)

    const trigger = screen.getByRole("button", { name: "登录并打开论文库" })
    expect(screen.queryByRole("link", { name: "创建或登录本机账号" })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: "管理/清除本机数据" })).not.toBeInTheDocument()
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





