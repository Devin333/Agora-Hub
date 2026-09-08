import { fireEvent, render, screen, within } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { AppShell } from "@/components/layout/app-shell"
import { useUiStore } from "@/stores/ui-store"

const navigationState = vi.hoisted(() => ({
  pathname: "/"
}))

vi.mock("next/navigation", () => ({
  usePathname: () => navigationState.pathname
}))

vi.mock("@/components/auth/account-menu", () => ({
  AccountMenu: () => null
}))

describe("AppShell", () => {
  beforeEach(() => {
    navigationState.pathname = "/"
    useUiStore.setState({ locale: "en", theme: "light" })
  })

  it.each(["/papers/methods", "/papers/tasks"])("allows the redesigned directory to own its header on %s", pathname => {
    navigationState.pathname = pathname
    render(<AppShell><div>Directory content</div></AppShell>)
    expect(screen.getByText("Directory content")).toBeInTheDocument()
    expect(screen.queryByRole("link", { name: "Agora Hub Research" })).not.toBeInTheDocument()
    expect(screen.queryByRole("main")).not.toBeInTheDocument()
  })

  it.each(["/", "/news", "/community", "/reports", "/search", "/papers", "/papers/tasks/agents", "/papers/methods/tool-use", "/projects", "/projects/hot", "/projects/tools/openai-codex"])("renders the Research header on portal route %s", (pathname) => {
    navigationState.pathname = pathname

    render(
      <AppShell>
        <div>Portal content</div>
      </AppShell>
    )

    expect(screen.getByRole("link", { name: "Agora Hub Research" })).toHaveAttribute("href", "/papers")
    expect(screen.getByRole("button", { name: "Research" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Projects" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Today" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Trends" })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Reports" })).toBeInTheDocument()
    expect(screen.getByRole("navigation", { name: "Portal modules" })).not.toHaveAttribute("style")
    expect(screen.getByPlaceholderText(/search papers/i)).not.toHaveAttribute("style")
    expect(screen.getByPlaceholderText(/search papers/i)).toBeInTheDocument()
    expect(screen.queryByRole("link", { name: "Today" })).not.toBeInTheDocument()
  })

  it("exposes Projects product routes from the desktop Research header", () => {
    navigationState.pathname = "/projects"

    render(
      <AppShell>
        <div>Projects content</div>
      </AppShell>
    )

    fireEvent.click(screen.getByRole("button", { name: "Projects" }))

    expect(screen.getByRole("button", { name: "Projects" })).toHaveAttribute("data-current", "true")
    expect(screen.getByRole("link", { name: /^Projects\b/, current: "page" })).toHaveAttribute("href", "/projects")
    expect(screen.getByRole("link", { name: /^Hot Projects\b/ })).toHaveAttribute("href", "/projects/hot")
    expect(screen.getByRole("link", { name: /^Rising Projects\b/ })).toHaveAttribute("href", "/projects/rising")
    expect(screen.getByRole("link", { name: /^Tools\b/ })).toHaveAttribute("href", "/projects/tools")
    expect(screen.getByRole("link", { name: /^Cases\b/ })).toHaveAttribute("href", "/projects/cases")
    expect(screen.getByRole("link", { name: /^Lab\b/ })).toHaveAttribute("href", "/projects/lab")
    expect(screen.getByRole("link", { name: /^Collections\b/ })).toHaveAttribute("href", "/projects/collections")
    expect(screen.getByRole("link", { name: /^Watchlist\b/ })).toHaveAttribute("href", "/projects/watchlist")
  })

  it("marks the closest Projects section as current on nested project routes", () => {
    navigationState.pathname = "/projects/tools/openai-codex"

    render(
      <AppShell>
        <div>Project tool content</div>
      </AppShell>
    )

    fireEvent.click(screen.getByRole("button", { name: "Projects" }))

    expect(screen.getByRole("button", { name: "Projects" })).toHaveAttribute("data-current", "true")
    expect(screen.getByRole("link", { name: /^Tools\b/, current: "page" })).toHaveAttribute("href", "/projects/tools")
    expect(screen.getByRole("link", { name: /^Projects\b/ })).not.toHaveAttribute("aria-current")
  })

  it("exposes Projects product routes from the mobile Research navigation", () => {
    navigationState.pathname = "/projects/hot"

    render(
      <AppShell>
        <div>Projects content</div>
      </AppShell>
    )

    fireEvent.click(screen.getByRole("button", { name: "Open Research navigation" }))

    const mobileNavigation = within(screen.getByRole("navigation", { name: "Research mobile navigation" }))
    expect(mobileNavigation.getByRole("link", { name: "Projects" })).toHaveAttribute("href", "/projects")
    expect(mobileNavigation.getByRole("link", { name: "Hot Projects", current: "page" })).toHaveAttribute("href", "/projects/hot")
    expect(mobileNavigation.getByRole("link", { name: "Tools" })).toHaveAttribute("href", "/projects/tools")
    expect(mobileNavigation.queryByRole("link", { name: "Open Source" })).not.toBeInTheDocument()
  })

  it.each(["/admin", "/studio", "/studio/runs"])("keeps management route %s outside the portal Research header", (pathname) => {
    navigationState.pathname = pathname

    render(
      <AppShell>
        <div>Management content</div>
      </AppShell>
    )

    expect(screen.getByText("Management content")).toBeInTheDocument()
    expect(screen.queryByRole("link", { name: "Agora Hub Research" })).not.toBeInTheDocument()
  })

  it.each(["/papers/reader-paper", "/papers/reader-paper/read"])("keeps paper reader route %s outside the portal Research header and frame", (pathname) => {
    navigationState.pathname = pathname

    const { container } = render(
      <AppShell>
        <div>Open Reader content</div>
      </AppShell>
    )

    expect(screen.getByText("Open Reader content")).toBeInTheDocument()
    expect(screen.queryByRole("link", { name: "Agora Hub Research" })).not.toBeInTheDocument()
    expect(container.querySelector("main")).toBeNull()
  })

  it("keeps the admin surface outside the portal Research header", () => {
    navigationState.pathname = "/news"

    render(
      <AppShell surface="admin">
        <div>Admin surface content</div>
      </AppShell>
    )

    expect(screen.getByText("Admin surface content")).toBeInTheDocument()
    expect(screen.queryByRole("link", { name: "Agora Hub Research" })).not.toBeInTheDocument()
  })
})
