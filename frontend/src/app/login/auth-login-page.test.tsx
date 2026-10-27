import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import LoginPage from "@/app/login/page"
import { bootstrapAccount, fetchAuthSession, login } from "@/lib/auth/api"

const navigation = vi.hoisted(() => ({
  refresh: vi.fn(),
  replace: vi.fn(),
  searchParams: new URLSearchParams()
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

describe("LoginPage", () => {
  const originalSurface = process.env.NEWSROOM_FRONTEND_SURFACE

  beforeEach(() => {
    delete process.env.NEWSROOM_FRONTEND_SURFACE
    navigation.refresh.mockReset()
    navigation.replace.mockReset()
    navigation.searchParams = new URLSearchParams()
    vi.mocked(bootstrapAccount).mockReset()
    vi.mocked(fetchAuthSession).mockReset()
    vi.mocked(login).mockReset()
  })

  afterEach(() => {
    if (originalSurface === undefined) {
      delete process.env.NEWSROOM_FRONTEND_SURFACE
      return
    }
    process.env.NEWSROOM_FRONTEND_SURFACE = originalSurface
  })

  it("shows first-admin bootstrap when auth is uninitialized", async () => {
    vi.mocked(fetchAuthSession).mockResolvedValue({ initialized: false, session: null })
    vi.mocked(bootstrapAccount).mockResolvedValue({
      user: { userId: "user-1", username: "admin", role: "admin" },
      sessionId: "sess-1",
      expiresAt: "2026-06-01T00:00:00Z"
    })

    render(<LoginPage />)

    const heading = await screen.findByRole("heading", { name: "创建第一个 Agora Hub 账号" })
    expect(heading).toHaveClass("text-4xl", "font-medium", "tracking-tight")
    const form = heading.closest("form")!
    expect(form).toHaveClass("max-w-[460px]")
    expect(form.querySelector('[data-slot="brand-mark"]')).toHaveStyle({ width: "74px", height: "74px" })
    expect(screen.getByLabelText("用户名")).toHaveAttribute("data-size", "xl")
    expect(screen.getByLabelText("密码")).toHaveAttribute("data-size", "xl")
    expect(screen.getByRole("button", { name: "创建账号" })).toHaveClass("w-full")
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "admin" } })
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "correct horse" } })
    fireEvent.submit(screen.getByRole("button", { name: "创建账号" }).closest("form")!)

    await waitFor(() => {
      expect(bootstrapAccount).toHaveBeenCalledWith("admin", "correct horse")
    })
    expect(navigation.replace).toHaveBeenCalledWith("/")
    expect(navigation.refresh).toHaveBeenCalled()
  })

  it("shows login after initialization and respects safe next paths", async () => {
    navigation.searchParams = new URLSearchParams({ next: "/papers/reader-paper" })
    vi.mocked(fetchAuthSession).mockResolvedValue({ initialized: true, session: null })
    vi.mocked(login).mockResolvedValue({
      user: { userId: "user-1", username: "admin", role: "admin" },
      sessionId: "sess-1",
      expiresAt: "2026-06-01T00:00:00Z"
    })

    render(<LoginPage />)

    expect(await screen.findByRole("heading", { name: "登录 Agora Hub" })).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "admin" } })
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "correct horse" } })
    fireEvent.submit(screen.getByRole("button", { name: "登录" }).closest("form")!)

    await waitFor(() => {
      expect(login).toHaveBeenCalledWith("admin", "correct horse")
    })
    expect(navigation.replace).toHaveBeenCalledWith("/papers/reader-paper")
  })

  it("redirects an existing session away from login", async () => {
    navigation.searchParams = new URLSearchParams({ next: "//evil.example" })
    vi.mocked(fetchAuthSession).mockResolvedValue({
      initialized: true,
      session: {
        user: { userId: "user-1", username: "admin", role: "admin" },
        sessionId: "sess-1",
        expiresAt: "2026-06-01T00:00:00Z"
      }
    })

    render(<LoginPage />)

    await waitFor(() => {
      expect(navigation.replace).toHaveBeenCalledWith("/")
    })
  })

  it("uses the Admin home as the default login target in Admin surface mode", async () => {
    process.env.NEWSROOM_FRONTEND_SURFACE = "admin"
    vi.mocked(fetchAuthSession).mockResolvedValue({
      initialized: true,
      session: {
        user: { userId: "user-1", username: "admin", role: "admin" },
        sessionId: "sess-1",
        expiresAt: "2026-06-01T00:00:00Z"
      }
    })

    render(<LoginPage />)

    await waitFor(() => {
      expect(navigation.replace).toHaveBeenCalledWith("/")
    })
  })
})
