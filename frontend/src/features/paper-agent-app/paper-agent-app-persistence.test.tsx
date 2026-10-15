import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { AuthSession } from "@/lib/papers/types"
import type { ResearchSnapshot } from "@/lib/research/history-model"
import type { ResearchResult } from "@/lib/research/conversation"
import type { useGuidedResearch } from "@/lib/research/use-guided-research"
import {
  acceptAccountHistory,
  acknowledgeAccountHistory,
  historyState,
  saveResearchMaterial,
  selectHistoryOwner,
  setHistoryStatus,
} from "@/lib/research/history"
import { retryHistoryEvent, startHistorySync } from "@/lib/research/history-sync"
import type { ResearchMaterial } from "@/lib/research/workspace-items"
import { PaperAgentApp } from "./paper-agent-app"

const mocks = vi.hoisted(() => ({
  account: {
    resolved: true,
    session: null as AuthSession | null,
    pending: false,
    error: null,
    openLogin: vi.fn(),
    signOut: vi.fn(),
  },
  guided: {
    active: null as ReturnType<typeof useGuidedResearch>["active"],
    busy: false,
    saveError: "",
    start: vi.fn(),
  },
}))

vi.mock("@/components/auth/portal-account-provider", () => ({ usePortalAccount: () => mocks.account }))
vi.mock("@/lib/research/use-guided-research", () => ({ useGuidedResearch: () => mocks.guided }))
vi.mock("@/lib/research/desktop/client", () => ({
  fetchDesktopCapabilities: vi.fn(() => new Promise(() => {})),
  fetchDesktopIdentity: vi.fn(() => new Promise(() => {})),
}))
vi.mock("./desktop-lifecycle", () => ({ useDesktopLifecycle: vi.fn() }))
vi.mock("./personal-memory-panel", () => ({ PersonalMemoryPanel: () => null }))
vi.mock("./paper-agent-settings-panel", () => ({ PaperAgentSettingsModal: () => null }))
vi.mock("./paper-reader-pane", () => ({ PaperReaderPane: () => null }))
// Reproduce the actual child import handoff: write the real history store without an App receipt.
vi.mock("@/features/portal/components/research-source-entry", () => ({
  ResearchSourceEntry: ({ onAttach }: { onAttach: (id: string) => void }) => (
    <button type="button" onClick={() => {
      if (saveResearchMaterial(imported, "user-1")) onAttach(imported.id)
    }}>测试导入交接</button>
  ),
}))

const imported: ResearchMaterial = {
  id: "import-1", kind: "pdf", title: "Imported.pdf", url: "", notes: "", groupId: null,
  referenceId: "private-paper-1", readerHref: "/papers/private-paper-1/read",
  createdAt: 1, updatedAt: 2,
}
const onlineResult: ResearchResult = {
  id: "arxiv:2601.01234", kind: "papers", title: "Online paper", description: "Verified source",
  source: "arXiv", url: "https://arxiv.org/abs/2601.01234", href: "/papers/2601.01234",
}
const onlineMaterial: ResearchMaterial = {
  ...imported, id: "online-1", kind: "paper", title: onlineResult.title,
  url: onlineResult.url, referenceId: "2601.01234", readerHref: "/papers/2601.01234/read",
}

function session(userId = "user-1"): AuthSession {
  return { user: { userId, username: userId, role: "user" }, sessionId: `session-${userId}`, expiresAt: "2099-01-01T00:00:00Z" }
}

function snapshot(materials: ResearchMaterial[] = [], revision = 4): ResearchSnapshot {
  return { visits: [], groups: [], materials, revision }
}

function historyResponse(data: ResearchSnapshot, status = 200) {
  return new Response(JSON.stringify(status === 200 ? { success: true, data } : { success: false, error: { code: "history_write_failed" } }), {
    status, headers: { "Content-Type": "application/json" },
  })
}

function libraryLabel(title = imported.title) {
  const button = screen.getByRole("button", { name: `选择论文：${title}` })
  const label = button.querySelector("[data-save-state]")
  if (!label) throw new Error("Library item must expose its persistence state")
  return label
}

function loadWorkspace(materials: ResearchMaterial[] = [], owner = "user-1") {
  selectHistoryOwner(owner)
  acceptAccountHistory(snapshot(materials), 4)
}

function showOnlineResult() {
  mocks.guided.active = {
    id: "search-1", owner: "user-1", groupId: null,
    conversation: {
      version: 1, mode: "auto", draft: "", materialIds: [],
      turns: [{
        id: "turn-1", question: "Find papers", answers: [], phase: "results", createdAt: 1,
        searches: [{ source: "papers", results: [onlineResult], total: 1 }], failures: [],
        events: [{ phase: "results", at: 1 }],
      }],
    },
  }
}

let stopSync: (() => void) | undefined

beforeEach(() => {
  vi.stubEnv("NEWSROOM_DEPLOYMENT_PROFILE", "local-desktop")
  vi.stubEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", "local-desktop")
  localStorage.clear()
  sessionStorage.clear()
  selectHistoryOwner(null)
  mocks.account.session = session()
  mocks.guided.active = null
  mocks.guided.start.mockClear()
})

afterEach(() => {
  stopSync?.()
  stopSync = undefined
  cleanup()
  selectHistoryOwner(null)
  vi.unstubAllGlobals()
  vi.unstubAllEnvs()
})

describe("PaperAgentApp persistence acknowledgements", () => {
  it("keeps a receipt-free imported material pending until the real history sync write is acknowledged", async () => {
    let finishWrite!: (response: Response) => void
    const write = new Promise<Response>(resolve => { finishWrite = resolve })
    const fetcher = vi.fn()
      .mockResolvedValueOnce(historyResponse(snapshot()))
      .mockReturnValueOnce(write)
    vi.stubGlobal("fetch", fetcher)
    stopSync = startHistorySync("user-1")
    await waitFor(() => expect(historyState().ready).toBe(true))
    render(<PaperAgentApp />)

    fireEvent.click(screen.getByRole("button", { name: "测试导入交接" }))
    expect(libraryLabel()).toHaveAttribute("data-save-state", "pending")
    expect(libraryLabel()).toHaveTextContent("待保存")
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(2))
    expect(historyState()).toMatchObject({ status: "saving", dirty: true, revision: 4 })
    expect(libraryLabel()).not.toHaveTextContent("已保存")
    const [, put] = fetcher.mock.calls[1] as [string, RequestInit]
    expect(put.method).toBe("PUT")
    const submitted = JSON.parse(String(put.body)) as ResearchSnapshot
    expect(submitted.materials).toContainEqual(imported)

    finishWrite(historyResponse({ ...submitted, revision: 5 }))
    await waitFor(() => expect(libraryLabel()).toHaveAttribute("data-save-state", "confirmed"))
    expect(historyState()).toMatchObject({ status: "synced", dirty: false, revision: 5 })
  })

  it.each([500, 409])("retries an imported %s failure without dereferencing an absent receipt or repeating the import", async status => {
    let finishRetry!: (response: Response) => void
    const retry = new Promise<Response>(resolve => { finishRetry = resolve })
    const fetcher = vi.fn()
      .mockResolvedValueOnce(historyResponse(snapshot()))
      .mockResolvedValueOnce(historyResponse(snapshot(), status))
      .mockReturnValueOnce(retry)
    vi.stubGlobal("fetch", fetcher)
    stopSync = startHistorySync("user-1")
    await waitFor(() => expect(historyState().ready).toBe(true))
    render(<PaperAgentApp />)
    fireEvent.click(screen.getByRole("button", { name: "测试导入交接" }))
    await waitFor(() => expect(libraryLabel()).toHaveAttribute("data-save-state", "failed"))
    expect(libraryLabel()).toHaveTextContent("保存失败")
    const generation = historyState().generation
    const retained = historyState().workspace.materials

    fireEvent.click(screen.getByRole("button", { name: "重试保存" }))
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3))
    expect(libraryLabel()).toHaveAttribute("data-save-state", "pending")
    expect(historyState().generation).toBe(generation)
    expect(historyState().workspace.materials).toEqual(retained)
    const [, put] = fetcher.mock.calls[2] as [string, RequestInit]
    const submitted = JSON.parse(String(put.body)) as ResearchSnapshot
    expect(put.method).toBe("PUT")
    expect(submitted.materials).toEqual([imported])

    finishRetry(historyResponse({ ...submitted, revision: 5 }))
    await waitFor(() => expect(libraryLabel()).toHaveAttribute("data-save-state", "confirmed"))
    expect(screen.queryByRole("button", { name: "重试保存" })).not.toBeInTheDocument()
  })

  it.each([500, 409])("retains the strong receipt when retrying a %s failure after an App edit", async status => {
    let finishRetry!: (response: Response) => void
    const retry = new Promise<Response>(resolve => { finishRetry = resolve })
    const fetcher = vi.fn()
      .mockResolvedValueOnce(historyResponse(snapshot([imported])))
      .mockResolvedValueOnce(historyResponse(snapshot(), status))
      .mockReturnValueOnce(retry)
    vi.stubGlobal("fetch", fetcher)
    stopSync = startHistorySync("user-1")
    await waitFor(() => expect(historyState().ready).toBe(true))
    render(<PaperAgentApp />)
    fireEvent.click(screen.getByRole("button", { name: "收藏" }))
    await waitFor(() => expect(libraryLabel()).toHaveAttribute("data-save-state", "failed"))
    fireEvent.click(screen.getByRole("button", { name: "重试保存" }))
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3))
    expect(libraryLabel()).toHaveAttribute("data-save-state", "pending")
    const [, put] = fetcher.mock.calls[2] as [string, RequestInit]
    const submitted = JSON.parse(String(put.body)) as ResearchSnapshot
    expect(submitted.materials?.[0]).toMatchObject({ id: imported.id, favorite: true })
    finishRetry(historyResponse({ ...submitted, revision: 5 }))
    await waitFor(() => expect(libraryLabel()).toHaveAttribute("data-save-state", "confirmed"))
  })

  it("keeps an unresolved import revision conflict failed after a rejected retry", async () => {
    const fetcher = vi.fn()
      .mockResolvedValueOnce(historyResponse(snapshot()))
      .mockResolvedValueOnce(historyResponse(snapshot(), 409))
      .mockResolvedValueOnce(historyResponse(snapshot(), 409))
    vi.stubGlobal("fetch", fetcher)
    stopSync = startHistorySync("user-1")
    await waitFor(() => expect(historyState().ready).toBe(true))
    render(<PaperAgentApp />)
    fireEvent.click(screen.getByRole("button", { name: "测试导入交接" }))
    await waitFor(() => expect(libraryLabel()).toHaveAttribute("data-save-state", "failed"))
    fireEvent.click(screen.getByRole("button", { name: "重试保存" }))
    await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3))
    await waitFor(() => expect(historyState().status).toBe("conflict"))
    expect(libraryLabel()).toHaveAttribute("data-save-state", "failed")
    expect(historyState()).toMatchObject({ dirty: true, revision: 4 })
    expect(historyState().workspace.materials).toEqual([imported])
  })

  it("honestly renders existing online results without an App receipt through pending, failure and acknowledgement", () => {
    loadWorkspace([onlineMaterial])
    showOnlineResult()
    render(<PaperAgentApp />)
    expect(screen.getByRole("button", { name: "已入库" })).toBeDisabled()

    act(() => { saveResearchMaterial({ ...onlineMaterial, updatedAt: 3 }, "user-1") })
    expect(libraryLabel(onlineMaterial.title)).toHaveAttribute("data-save-state", "pending")
    expect(screen.getByRole("button", { name: "入库待保存" })).toBeDisabled()
    expect(screen.queryByRole("button", { name: "已入库" })).not.toBeInTheDocument()
    act(() => { setHistoryStatus("error", "write failed") })
    expect(screen.getByRole("button", { name: "保存失败·重试" })).toBeEnabled()
    expect(libraryLabel(onlineMaterial.title)).toHaveAttribute("data-save-state", "failed")
    act(() => { acknowledgeAccountHistory(5, historyState().generation) })
    expect(libraryLabel(onlineMaterial.title)).toHaveAttribute("data-save-state", "confirmed")
    expect(screen.getByRole("button", { name: "已入库" })).toBeDisabled()
  })

  it("retries a receipt-free online record through history sync without rewriting its material snapshot", () => {
    loadWorkspace([onlineMaterial])
    showOnlineResult()
    render(<PaperAgentApp />)
    act(() => { saveResearchMaterial({ ...onlineMaterial, updatedAt: 3 }, "user-1") })
    act(() => { setHistoryStatus("conflict", "revision conflict") })
    const retained = historyState().workspace.materials
    const generation = historyState().generation
    const onRetry = vi.fn()
    window.addEventListener(retryHistoryEvent, onRetry)
    try {
      fireEvent.click(screen.getByRole("button", { name: "保存失败·重试" }))
      expect(onRetry).toHaveBeenCalledOnce()
      expect(historyState().workspace.materials).toEqual(retained)
      expect(historyState().generation).toBe(generation)
      expect(libraryLabel(onlineMaterial.title)).toHaveAttribute("data-save-state", "failed")
      expect(screen.queryByRole("button", { name: "已入库" })).not.toBeInTheDocument()
    } finally {
      window.removeEventListener(retryHistoryEvent, onRetry)
    }
  })

  it("confirms loaded records but cannot confirm a receipt at its original base revision", () => {
    loadWorkspace([imported])
    render(<PaperAgentApp />)
    expect(libraryLabel()).toHaveAttribute("data-save-state", "confirmed")
    fireEvent.click(screen.getByRole("button", { name: "收藏" }))
    expect(libraryLabel()).toHaveAttribute("data-save-state", "pending")
    act(() => { acknowledgeAccountHistory(4, historyState().generation) })
    expect(libraryLabel()).toHaveAttribute("data-save-state", "pending")
    act(() => { acknowledgeAccountHistory(5, historyState().generation) })
    expect(libraryLabel()).toHaveAttribute("data-save-state", "confirmed")
  })

  it("ignores another owner's receipts even when the new owner's loaded material has the same id", () => {
    loadWorkspace([imported])
    const view = render(<PaperAgentApp />)
    fireEvent.click(screen.getByRole("button", { name: "收藏" }))
    expect(libraryLabel()).toHaveAttribute("data-save-state", "pending")

    mocks.account.session = session("user-2")
    view.rerender(<PaperAgentApp />)
    expect(screen.queryByRole("button", { name: `选择论文：${imported.title}` })).not.toBeInTheDocument()
    act(() => { selectHistoryOwner("user-2") })
    expect(screen.queryByText("已保存", { exact: true })).not.toBeInTheDocument()
    act(() => { acceptAccountHistory(snapshot([{ ...imported, title: "Owner two.pdf" }]), 4) })
    expect(libraryLabel("Owner two.pdf")).toHaveAttribute("data-save-state", "confirmed")
    expect(screen.queryByRole("button", { name: `选择论文：${imported.title}` })).not.toBeInTheDocument()
  })

  it("does not dispatch a retry from a stale owner render before the hook processes an owner change", () => {
    loadWorkspace([imported])
    render(<PaperAgentApp />)
    act(() => { setHistoryStatus("error", "write failed") })
    const retry = screen.getByRole("button", { name: "重试保存" })
    const onRetry = vi.fn()
    window.addEventListener(retryHistoryEvent, onRetry)
    try {
      // An actual store owner change precedes the React rerender; the click must recheck that owner.
      act(() => {
        selectHistoryOwner("user-2")
        fireEvent.click(retry)
      })
      expect(onRetry).not.toHaveBeenCalled()
      expect(historyState().workspace.materials).toBeUndefined()
    } finally {
      window.removeEventListener(retryHistoryEvent, onRetry)
    }
  })
})
