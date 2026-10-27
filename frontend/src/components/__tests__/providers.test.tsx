import type { ReactNode } from "react"
import { act, render, screen } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { Providers } from "@/components/providers"
import { useUiStore } from "@/stores/ui-store"

const navigationState = vi.hoisted(() => ({ pathname: "/login" }))
const originalDeploymentProfile = process.env.NEWSROOM_DEPLOYMENT_PROFILE
const originalPublicDeploymentProfile = process.env.NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE

vi.mock("next/navigation", () => ({
  usePathname: () => navigationState.pathname,
}))

vi.mock("@/components/auth/portal-account-provider", () => ({
  PortalAccountProvider: ({ children }: { children: ReactNode }) => <div data-testid="portal-account-provider">{children}</div>,
  usePortalAccount: () => ({ resolved: true, session: null, error: null }),
}))

vi.mock("@/components/research/research-history-tracker", () => ({
  ResearchHistoryTracker: () => <div data-testid="research-history-runtime" />,
}))

vi.mock("@/lib/research/history-sync", () => ({
  startHistorySync: vi.fn(),
}))

vi.mock("@/lib/research/history", () => ({
  setHistoryStatus: vi.fn(),
}))

vi.mock("@/features/portal/components/research-command-palette", () => ({
  ResearchCommandPalette: () => <div data-testid="research-command-palette" />,
}))

describe("Providers route isolation", () => {
  beforeEach(() => {
    navigationState.pathname = "/login"
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "default"
    delete process.env.NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE
    useUiStore.setState({ locale: "en", appearance: "light", theme: "light" })
    delete document.documentElement.dataset.theme
    document.documentElement.lang = "en"
  })

  afterEach(() => {
    restoreEnv("NEWSROOM_DEPLOYMENT_PROFILE", originalDeploymentProfile)
    restoreEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", originalPublicDeploymentProfile)
    delete document.documentElement.dataset.theme
    document.documentElement.style.colorScheme = ""
    document.documentElement.lang = "en"
    vi.unstubAllGlobals()
  })

  it.each(["/login", "/login/"])("suppresses legacy global commands on exact hosted login route %s without bypassing shared providers", (pathname) => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "hosted"
    navigationState.pathname = pathname
    useUiStore.setState({ locale: "zh", appearance: "dark" })

    render(<Providers><div>Hosted login</div></Providers>)

    expect(screen.getByText("Hosted login")).toBeInTheDocument()
    expect(screen.getByTestId("portal-account-provider")).toBeInTheDocument()
    expect(screen.getByTestId("research-history-runtime")).toBeInTheDocument()
    expect(screen.queryByTestId("research-command-palette")).not.toBeInTheDocument()
    expect(document.documentElement.dataset.theme).toBe("dark")
    expect(document.documentElement.style.colorScheme).toBe("dark")
    expect(useUiStore.getState().theme).toBe("dark")
    expect(document.documentElement.lang).toBe("zh-CN")
  })

  it("follows the platform color scheme while the appearance is system", () => {
    const listeners = new Set<(event: MediaQueryListEvent) => void>()
    const query = {
      matches: true,
      addEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
      removeEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener),
    }
    vi.stubGlobal("matchMedia", vi.fn(() => query))
    useUiStore.setState({ appearance: "system" })

    render(<Providers><div>Portal</div></Providers>)
    expect(document.documentElement.dataset.theme).toBe("light")

    act(() => {
      query.matches = false
      listeners.forEach((listener) => listener({ matches: false } as MediaQueryListEvent))
    })
    expect(document.documentElement.dataset.theme).toBe("dark")
    expect(useUiStore.getState().theme).toBe("dark")

    act(() => useUiStore.getState().setTheme("light"))
    expect(listeners.size).toBe(0)
    expect(document.documentElement.dataset.theme).toBe("light")
  })

  it("keeps global commands on the ordinary Portal login route", () => {
    render(<Providers><div>Portal login</div></Providers>)

    expect(screen.getByTestId("research-command-palette")).toBeInTheDocument()
  })

  it("does not suppress global commands on a similarly prefixed hosted route", () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "hosted"
    navigationState.pathname = "/login/help"

    render(<Providers><div>Login help</div></Providers>)

    expect(screen.getByTestId("research-command-palette")).toBeInTheDocument()
  })
})

function restoreEnv(name: string, value: string | undefined) {
  if (value === undefined) {
    delete process.env[name]
    return
  }
  process.env[name] = value
}
