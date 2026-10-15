import type { ReactNode } from "react"
import { render, screen } from "@testing-library/react"
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
    useUiStore.setState({ locale: "en", theme: "light" })
    document.documentElement.classList.remove("dark")
    delete document.documentElement.dataset.theme
    document.documentElement.lang = "en"
  })

  afterEach(() => {
    restoreEnv("NEWSROOM_DEPLOYMENT_PROFILE", originalDeploymentProfile)
    restoreEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", originalPublicDeploymentProfile)
    document.documentElement.classList.remove("dark")
    delete document.documentElement.dataset.theme
    document.documentElement.lang = "en"
  })

  it.each(["/login", "/login/"])("suppresses legacy global commands on exact local-desktop login route %s without bypassing shared providers", (pathname) => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "local-desktop"
    navigationState.pathname = pathname
    useUiStore.setState({ locale: "zh", theme: "dark" })

    render(<Providers><div>Desktop login</div></Providers>)

    expect(screen.getByText("Desktop login")).toBeInTheDocument()
    expect(screen.getByTestId("portal-account-provider")).toBeInTheDocument()
    expect(screen.getByTestId("research-history-runtime")).toBeInTheDocument()
    expect(screen.queryByTestId("research-command-palette")).not.toBeInTheDocument()
    expect(document.documentElement).toHaveClass("dark")
    expect(document.documentElement.dataset.theme).toBe("dark")
    expect(document.documentElement.lang).toBe("zh-CN")
  })

  it("keeps global commands on the ordinary Portal login route", () => {
    render(<Providers><div>Portal login</div></Providers>)

    expect(screen.getByTestId("research-command-palette")).toBeInTheDocument()
  })

  it("does not suppress global commands on a similarly prefixed local-desktop route", () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "local-desktop"
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
