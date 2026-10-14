import type { ReactNode } from "react"
import { act, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { Providers } from "@/components/providers"
import { usePortalAccount } from "@/components/auth/portal-account-provider"
import { apiPost } from "@/lib/api/client"
import { login } from "@/lib/auth/api"
import { fetchPortalSession } from "@/lib/auth/portal-api"
import { startHistorySync } from "@/lib/research/history-sync"

vi.mock("next/navigation", () => ({
  usePathname: () => "/paper-agent",
  useSearchParams: () => new URLSearchParams()
}))

vi.mock("@/lib/api/client", () => ({
  apiGet: vi.fn(),
  apiPost: vi.fn()
}))

vi.mock("@/lib/auth/portal-api", async (original) => ({
  ...await original<typeof import("@/lib/auth/portal-api")>(),
  fetchPortalSession: vi.fn()
}))

vi.mock("@/components/auth/portal-login-dialog", () => ({
  PortalLoginDialog: () => null
}))

vi.mock("@/components/research/research-history-tracker", () => ({
  ResearchHistoryTracker: () => null
}))

vi.mock("@/lib/research/history-sync", () => ({
  startHistorySync: vi.fn()
}))

vi.mock("@/lib/research/history", () => ({
  setHistoryStatus: vi.fn()
}))

vi.mock("@/features/portal/components/research-command-palette", () => ({
  ResearchCommandPalette: () => null
}))

vi.mock("@/components/ui/tooltip", () => ({
  TooltipProvider: ({ children }: { children: ReactNode }) => children
}))

const session = {
  user: { userId: "user-1", username: "admin", role: "admin" as const },
  sessionId: "sess-fresh",
  expiresAt: "2026-12-31T00:00:00Z"
}

function AccountOwner() {
  const account = usePortalAccount()
  return <div data-testid="account-owner">{account?.resolved ? account.session?.user.userId ?? "guest" : "pending"}</div>
}

describe("PortalAccountProvider auth synchronization", () => {
  beforeEach(() => {
    vi.mocked(apiPost).mockReset()
    vi.mocked(fetchPortalSession).mockReset()
    vi.mocked(startHistorySync).mockReset()
  })

  it("moves login into the fresh root context and history owner without accepting the stale initial guest", async () => {
    let initialSignal: AbortSignal | undefined
    let resolveInitial!: (value: { session: null }) => void
    const initialGuest = new Promise<{ session: null }>((resolve) => { resolveInitial = resolve })
    vi.mocked(fetchPortalSession)
      .mockImplementationOnce((signal) => {
        initialSignal = signal
        return initialGuest
      })
      .mockResolvedValueOnce({ session })
    vi.mocked(apiPost).mockResolvedValueOnce({ success: true, data: { session } })

    render(<Providers><AccountOwner /></Providers>)
    await waitFor(() => expect(fetchPortalSession).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId("account-owner")).toHaveTextContent("pending")

    await act(async () => {
      await login("admin", "correct horse")
    })

    await waitFor(() => expect(screen.getByTestId("account-owner")).toHaveTextContent("user-1"))
    await waitFor(() => expect(startHistorySync).toHaveBeenCalledWith("user-1"))
    expect(initialSignal?.aborted).toBe(true)

    await act(async () => {
      resolveInitial({ session: null })
      await initialGuest
    })

    expect(screen.getByTestId("account-owner")).toHaveTextContent("user-1")
    expect(vi.mocked(startHistorySync).mock.calls.map(([owner]) => owner)).not.toContain(null)
  })
})
