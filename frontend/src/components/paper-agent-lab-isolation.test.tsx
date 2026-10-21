import type { ReactNode } from "react"
import { cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { AppShell } from "@/components/layout/app-shell"
import { Providers } from "@/components/providers"
import { useUiStore } from "@/stores/ui-store"

const runtime = vi.hoisted(() => ({
  pathname: "/",
  portalProvider: vi.fn(),
  historyTracker: vi.fn(),
  commandPalette: vi.fn(),
  researchHeader: vi.fn(),
  startHistorySync: vi.fn(() => () => undefined),
}))

vi.mock("next/navigation", () => ({
  usePathname: () => runtime.pathname,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}))

vi.mock("@/components/auth/portal-account-provider", () => ({
  PortalAccountProvider: ({ children }: { children: ReactNode }) => {
    runtime.portalProvider()
    return <div data-testid="portal-account-provider">{children}</div>
  },
  usePortalAccount: () => ({
    session: {
      sessionId: "session-1",
      user: { userId: "ordinary-user", username: "Researcher", role: "user" },
    },
    resolved: true,
    pending: false,
    error: null,
  }),
}))

vi.mock("@/components/research/research-history-tracker", () => ({
  ResearchHistoryTracker: () => {
    runtime.historyTracker()
    return <div data-testid="research-history-tracker" />
  },
}))

vi.mock("@/features/portal/components/research-command-palette", () => ({
  ResearchCommandPalette: () => {
    runtime.commandPalette()
    return <div data-testid="global-command-palette" />
  },
}))

vi.mock("@/components/layout/research-header", () => ({
  ResearchHeader: () => {
    runtime.researchHeader()
    return <header data-testid="research-header" />
  },
}))

vi.mock("@/lib/research/history-sync", () => ({
  startHistorySync: runtime.startHistorySync,
}))

vi.mock("@/lib/deployment-profile", () => ({
  isLocalDesktopProfile: () => false,
}))

describe("paper agent lab provider isolation", () => {
  beforeEach(() => {
    runtime.pathname = "/"
    runtime.portalProvider.mockClear()
    runtime.historyTracker.mockClear()
    runtime.commandPalette.mockClear()
    runtime.researchHeader.mockClear()
    runtime.startHistorySync.mockClear()
    useUiStore.setState({ theme: "dark", locale: "zh" })
    document.documentElement.classList.remove("dark")
    document.documentElement.dataset.theme = "untouched"
    document.documentElement.lang = "x-preview"
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it.each(["/paper-agent-lab", "/paper-agent-lab/"])(
    "keeps the exact isolated route %s outside portal account, history, commands and theme side effects",
    async (pathname) => {
      runtime.pathname = pathname

      render(<Providers><main>Paper Agent Lab</main></Providers>)

      expect(screen.getByText("Paper Agent Lab")).toBeInTheDocument()
      expect(screen.queryByTestId("portal-account-provider")).not.toBeInTheDocument()
      expect(screen.queryByTestId("research-history-tracker")).not.toBeInTheDocument()
      expect(screen.queryByTestId("global-command-palette")).not.toBeInTheDocument()
      expect(runtime.portalProvider).not.toHaveBeenCalled()
      expect(runtime.historyTracker).not.toHaveBeenCalled()
      expect(runtime.commandPalette).not.toHaveBeenCalled()
      expect(runtime.startHistorySync).not.toHaveBeenCalled()

      await Promise.resolve()
      expect(document.documentElement).not.toHaveClass("dark")
      expect(document.documentElement).toHaveAttribute("data-theme", "untouched")
      expect(document.documentElement).toHaveAttribute("lang", "x-preview")
    },
  )

  it.each(["/papers", "/paper-agent-lab-notes", "/paper-agent-lab/preview"])(
    "retains the normal provider runtime on %s",
    async (pathname) => {
      runtime.pathname = pathname

      render(<Providers><main>Portal content</main></Providers>)

      expect(screen.getByText("Portal content")).toBeInTheDocument()
      expect(screen.getByTestId("portal-account-provider")).toBeInTheDocument()
      expect(screen.getByTestId("research-history-tracker")).toBeInTheDocument()
      expect(screen.getByTestId("global-command-palette")).toBeInTheDocument()
      expect(runtime.portalProvider).toHaveBeenCalled()
      expect(runtime.historyTracker).toHaveBeenCalled()
      expect(runtime.commandPalette).toHaveBeenCalled()
      await waitFor(() => expect(runtime.startHistorySync).toHaveBeenCalledWith("ordinary-user"))
      await waitFor(() => expect(document.documentElement).toHaveClass("dark"))
      expect(document.documentElement).toHaveAttribute("data-theme", "dark")
      expect(document.documentElement).toHaveAttribute("lang", "zh-CN")
    },
  )

  it.each(["/paper-agent-lab", "/paper-agent-lab/"])(
    "bypasses the portal AppShell chrome only for %s",
    (pathname) => {
      runtime.pathname = pathname

      const { container } = render(<AppShell><section>Isolated workspace</section></AppShell>)

      expect(screen.getByText("Isolated workspace")).toBeVisible()
      expect(screen.queryByTestId("research-header")).not.toBeInTheDocument()
      expect(runtime.researchHeader).not.toHaveBeenCalled()
      expect(container.firstElementChild).toHaveClass("min-h-screen")
      expect(container.querySelector("main")).not.toBeInTheDocument()
    },
  )

  it.each(["/paper-agent-lab-notes", "/paper-agent-lab/preview"])(
    "does not overmatch AppShell isolation for %s",
    (pathname) => {
      runtime.pathname = pathname

      render(<AppShell><section>Normal portal page</section></AppShell>)

      expect(screen.getByTestId("research-header")).toBeInTheDocument()
      expect(runtime.researchHeader).toHaveBeenCalledTimes(1)
      expect(screen.getByText("Normal portal page").closest("main")).not.toBeNull()
    },
  )

  it.each(["/paper-agent", "/paper-agent/"])(
    "keeps authenticated providers, owner history, and theme but removes global commands on %s",
    async (pathname) => {
      runtime.pathname = pathname

      render(<Providers><main>Personal paper agent</main></Providers>)

      expect(screen.getByTestId("portal-account-provider")).toBeInTheDocument()
      expect(screen.getByTestId("research-history-tracker")).toBeInTheDocument()
      expect(screen.queryByTestId("global-command-palette")).not.toBeInTheDocument()
      await waitFor(() => expect(runtime.startHistorySync).toHaveBeenCalledWith("ordinary-user"))
      await waitFor(() => expect(document.documentElement).toHaveClass("dark"))
      expect(document.documentElement).toHaveAttribute("data-theme", "dark")
      expect(document.documentElement).toHaveAttribute("lang", "zh-CN")
    },
  )

  it.each(["/paper-agent", "/paper-agent/"])(
    "bypasses only the legacy Portal chrome for the formal App route %s",
    (pathname) => {
      runtime.pathname = pathname

      const { container } = render(<AppShell><section>Formal paper agent</section></AppShell>)

      expect(screen.queryByTestId("research-header")).not.toBeInTheDocument()
      expect(container.querySelector("main")).not.toBeInTheDocument()
      expect(screen.getByText("Formal paper agent")).toBeVisible()
    },
  )

  it.each(["/paper-agent/settings", "/paper-agent-notes"])(
    "does not overmatch formal App isolation for %s",
    (pathname) => {
      runtime.pathname = pathname

      render(<AppShell><section>Ordinary prefixed route</section></AppShell>)

      expect(screen.getByTestId("research-header")).toBeInTheDocument()
      expect(screen.getByText("Ordinary prefixed route").closest("main")).not.toBeNull()
    },
  )
})