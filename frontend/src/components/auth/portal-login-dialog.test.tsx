import { act, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { PortalAccountProvider } from "./portal-account-provider"
import { PortalAccountControl } from "./portal-account-control"
import { validAuthorizationUrl } from "./portal-login-dialog"
import { authorizeProvider, fetchLoginMethods, fetchPortalSession, logoutPortal, requestOtp, verifyOtp, type AuthMethods } from "@/lib/auth/portal-api"
import { useState } from "react"

vi.mock("@/lib/auth/portal-api", async (original) => ({ ...await original<typeof import("@/lib/auth/portal-api")>(), fetchLoginMethods: vi.fn(), fetchPortalSession: vi.fn(), requestOtp: vi.fn(), verifyOtp: vi.fn(), authorizeProvider: vi.fn(), logoutPortal: vi.fn() }))

const available: AuthMethods = {
  methods: { phone: { available: true }, email: { available: true }, google: { available: true }, wechat: { available: true }, qq: { available: true } },
  termsUrl: "https://agora.test/terms", privacyUrl: "https://agora.test/privacy",
}
const session = { sessionId: "session", expiresAt: "2026-12-31T00:00:00Z", user: { userId: "ordinary-user", username: "Researcher", role: "user" } }

function Page() {
  const [draft, setDraft] = useState("研究问题")
  return <PortalAccountProvider><input aria-label="研究问题" value={draft} onChange={(event) => setDraft(event.target.value)} /><PortalAccountControl /></PortalAccountProvider>
}

async function open() {
  render(<Page />)
  fireEvent.click(screen.getByRole("button", { name: "登录" }))
  await screen.findByRole("dialog")
  await waitFor(() => expect(screen.queryByText("正在连接登录服务")).not.toBeInTheDocument())
}
function phone() {
  fireEvent.change(screen.getByLabelText("手机号", { selector: "input" }), { target: { value: "13800138000" } })
  fireEvent.click(screen.getByRole("checkbox"))
}

describe("portal login modal", () => {
  beforeEach(() => {
    vi.mocked(fetchLoginMethods).mockReset().mockResolvedValue(available)
    vi.mocked(fetchPortalSession).mockReset().mockResolvedValue({ session: null })
    vi.mocked(requestOtp).mockReset().mockResolvedValue({ challengeId: "challenge_0123456789", expiresAt: new Date(Date.now() + 300000).toISOString(), resendAt: new Date(Date.now() + 60000).toISOString() })
    vi.mocked(verifyOtp).mockReset().mockResolvedValue({ session })
    vi.mocked(authorizeProvider).mockReset()
    vi.mocked(logoutPortal).mockReset().mockResolvedValue({ revoked: true })
  })
  afterEach(() => vi.restoreAllMocks())

  it("opens in place, closes with Escape, restores focus and preserves the research draft", async () => {
    await open()
    const trigger = screen.getByRole("button", { name: "登录", hidden: true })
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" })
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())
    expect(screen.getByLabelText("研究问题")).toHaveValue("研究问题")
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it("keeps phone and email drafts independent and shows all provider entries", async () => {
    await open()
    phone()
    fireEvent.mouseDown(screen.getByRole("tab", { name: "邮箱" }), { button: 0, ctrlKey: false })
    expect(screen.getByLabelText("邮箱地址")).toHaveValue("")
    fireEvent.change(screen.getByLabelText("邮箱地址"), { target: { value: "research@example.com" } })
    fireEvent.mouseDown(screen.getByRole("tab", { name: "手机号" }), { button: 0, ctrlKey: false })
    expect(screen.getByLabelText("手机号", { selector: "input" })).toHaveValue("13800138000")
    for (const provider of ["Google", "微信", "QQ"]) expect(screen.getByRole("button", { name: `使用 ${provider} 登录` })).toBeInTheDocument()
  })

  it("requires consent, sends only one code and logs in without navigation", async () => {
    await open()
    fireEvent.change(screen.getByLabelText("手机号", { selector: "input" }), { target: { value: "13800138000" } })
    fireEvent.click(screen.getByRole("button", { name: "获取验证码" }))
    expect(requestOtp).not.toHaveBeenCalled()
    expect(screen.getByRole("alert")).toHaveTextContent("请先阅读并同意")
    fireEvent.click(screen.getByRole("checkbox"))
    fireEvent.click(screen.getByRole("button", { name: "获取验证码" }))
    await waitFor(() => expect(requestOtp).toHaveBeenCalledWith("phone", "+8613800138000", expect.any(AbortSignal)))
    await waitFor(() => expect(screen.getByRole("button", { name: /后重发/ })).toBeDisabled())
    fireEvent.change(screen.getByLabelText("验证码"), { target: { value: "123456" } })
    fireEvent.click(screen.getByRole("button", { name: "登录 / 注册" }))
    await waitFor(() => expect(verifyOtp).toHaveBeenCalledWith("challenge_0123456789", "123456", expect.any(AbortSignal)))
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())
    expect(screen.getByRole("button", { name: "账户菜单" })).toHaveTextContent("Researcher")
    expect(screen.getByLabelText("研究问题")).toHaveValue("研究问题")
  })

  it("never submits a challenge after the destination changes", async () => {
    await open(); phone()
    fireEvent.click(screen.getByRole("button", { name: "获取验证码" }))
    await screen.findByText(/验证码已发送/)
    fireEvent.change(screen.getByLabelText("手机号", { selector: "input" }), { target: { value: "13900139000" } })
    fireEvent.change(screen.getByLabelText("验证码"), { target: { value: "123456" } })
    fireEvent.click(screen.getByRole("button", { name: "登录 / 注册" }))
    expect(verifyOtp).not.toHaveBeenCalled()
    expect(screen.getByRole("alert")).toHaveTextContent("当前账号")
  })

  it("shows truthful unconfigured states without fake requests or codes", async () => {
    vi.mocked(fetchLoginMethods).mockResolvedValue({ ...available, methods: Object.fromEntries(Object.keys(available.methods).map((key) => [key, { available: false }])) as AuthMethods["methods"] })
    await open()
    expect(screen.getByText("手机号登录暂未开放")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "获取验证码" })).toBeDisabled()
    fireEvent.click(screen.getByRole("button", { name: "使用 微信 登录" }))
    expect(screen.getByRole("alert")).toHaveTextContent("微信登录暂未开放")
    expect(authorizeProvider).not.toHaveBeenCalled()
    expect(requestOtp).not.toHaveBeenCalled()
  })

  it("accepts completion only from the correct provider window, origin and state", async () => {
    const popup = { location: { href: "" }, close: vi.fn(), closed: false } as unknown as Window
    vi.spyOn(window, "open").mockReturnValue(popup)
    vi.mocked(authorizeProvider).mockResolvedValue({ authorizationUrl: "https://accounts.google.com/o/oauth2/v2/auth?state=expected-state", expiresAt: new Date(Date.now() + 300000).toISOString() })
    await open(); phone()
    fireEvent.click(screen.getByRole("button", { name: "使用 Google 登录" }))
    await screen.findByText("等待授权完成")
    const message = { type: "agora-auth-complete", provider: "google", state: "expected-state", success: true }
    const calls = vi.mocked(fetchPortalSession).mock.calls.length
    act(() => { window.dispatchEvent(new MessageEvent("message", { origin: "https://foreign.test", source: popup, data: message })); window.dispatchEvent(new MessageEvent("message", { origin: window.location.origin, source: window, data: message })); window.dispatchEvent(new MessageEvent("message", { origin: window.location.origin, source: popup, data: { ...message, state: "bad" } })) })
    expect(fetchPortalSession).toHaveBeenCalledTimes(calls)
    vi.mocked(fetchPortalSession).mockResolvedValue({ session })
    act(() => { window.dispatchEvent(new MessageEvent("message", { origin: window.location.origin, source: popup, data: message })) })
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument())
  })

  it("offers a real redirect fallback when the popup is blocked", async () => {
    vi.spyOn(window, "open").mockReturnValue(null)
    vi.mocked(authorizeProvider).mockResolvedValue({ authorizationUrl: "https://graph.qq.com/oauth2.0/authorize?state=qq-state", expiresAt: new Date(Date.now() + 300000).toISOString() })
    await open(); phone()
    fireEvent.click(screen.getByRole("button", { name: "使用 QQ 登录" }))
    expect(await screen.findByRole("link", { name: "继续前往 QQ" })).toHaveAttribute("href", "https://graph.qq.com/oauth2.0/authorize?state=qq-state")
  })

  it("embeds only the real WeChat authorization page", async () => {
    vi.mocked(authorizeProvider).mockResolvedValue({ authorizationUrl: "https://open.weixin.qq.com/connect/qrconnect?state=wx-state", expiresAt: new Date(Date.now() + 300000).toISOString() })
    await open(); phone()
    fireEvent.click(screen.getByRole("button", { name: "使用 微信 登录" }))
    expect(await screen.findByTitle("微信官方扫码登录")).toHaveAttribute("src", "https://open.weixin.qq.com/connect/qrconnect?state=wx-state&self_redirect=true")
    fireEvent.click(screen.getByRole("button", { name: "其他登录方式" }))
    expect(screen.getByLabelText("手机号", { selector: "input" })).toHaveValue("13800138000")
  })

  it("aborts pending verification on close", async () => {
    let signal: AbortSignal | undefined
    vi.mocked(requestOtp).mockImplementation((_channel, _destination, value) => { signal = value; return new Promise(() => {}) })
    await open(); phone()
    fireEvent.click(screen.getByRole("button", { name: "获取验证码" }))
    fireEvent.click(screen.getByRole("button", { name: "关闭登录" }))
    await waitFor(() => expect(signal?.aborted).toBe(true))
  })

  it("rejects unexpected or insecure provider authorization hosts", () => {
    expect(validAuthorizationUrl("google", "https://accounts.google.com/o/oauth2/v2/auth?state=ok")).toBe(true)
    for (const url of ["javascript:alert(1)", "http://accounts.google.com/?state=ok", "https://accounts.google.com.evil.test/?state=ok", "https://accounts.google.com/?x=ok", "https://user@accounts.google.com/?state=ok"]) expect(validAuthorizationUrl("google", url)).toBe(false)
  })
})
