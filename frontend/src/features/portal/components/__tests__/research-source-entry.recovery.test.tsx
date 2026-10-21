import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import * as history from "@/lib/research/history"
import { emptyWorkspace } from "@/lib/research/history-model"
import type { ResearchMaterial, ResearchPendingOperation } from "@/lib/research/workspace-items"
import { normalizeDirectSource, ResearchSourceEntry } from "../research-source-entry"

vi.mock("@/components/auth/portal-account-provider", () => ({ usePortalAccount: () => null }))
vi.mock("@/lib/deployment-profile", () => ({ isLocalDesktopProfile: () => true }))
const generation = `gen_${"a".repeat(32)}`
const sourceRevision = `sha256:${"b".repeat(64)}`
const fingerprint = `sha256:${"c".repeat(64)}`
const pending: ResearchPendingOperation = { requestId: "request-durable-import", generation, kind: "import", fingerprint, semantic: { kind: "import", filename: "durable.pdf", sizeBytes: 128, sourceRevision, sourceFingerprint: sourceRevision }, status: "unknown", sourceRevision, createdAt: 1, updatedAt: 2 }
const receipt = { requestId: pending.requestId, generation, kind: "import", fingerprint, status: "passed", importId: "import-owned", paperId: "paper-owned", sourceRevision, result: { filename: "durable.pdf", paperId: "paper-owned", sourceRevision, sourceFingerprint: sourceRevision }, recoverable: false, automaticRetryAllowed: false }
const completed: ResearchPendingOperation = { ...pending, status: "passed", importId: "import-owned", paperId: "paper-owned", sourceFingerprint: sourceRevision }
const response = (data: unknown) => new Response(JSON.stringify({ success: true, data }))
const load = (operation = pending) => history.acceptAccountHistory({ ...emptyWorkspace(), pendingOperations: [{ ...operation }] }, 4)

beforeEach(() => { sessionStorage.clear(); history.selectHistoryOwner("owner-recovery"); load(); vi.stubGlobal("fetch", vi.fn()) })
afterEach(() => { cleanup(); history.selectHistoryOwner(null); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe("durable PDF import recovery", () => {
  it("restores an unknown request without a session receipt and performs no automatic request", async () => {
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={vi.fn()} />)
    expect(await screen.findByText("durable.pdf")).toBeVisible()
    expect(screen.getByText(/导入回执尚未确认/)).toBeVisible()
    expect(fetch).not.toHaveBeenCalled()
    expect(sessionStorage.getItem("agora-pdf-import:owner-recovery")).toBeNull()
  })

  it("checks the original identity, persists the verified result and attaches without a second upload", async () => {
    vi.mocked(fetch).mockResolvedValue(response(receipt))
    const onAttach = vi.fn()
    const first = render(<ResearchSourceEntry appearance="paper-agent" onAttach={onAttach} />)
    fireEvent.click(await screen.findByRole("button", { name: "检查已有请求" }))
    await screen.findByText("文本已转换，可用于本次研究")
    expect(history.historyState().workspace.pendingOperations?.[0]).toMatchObject({ paperId: "paper-owned", importId: "import-owned", sourceRevision, sourceFingerprint: sourceRevision, status: "passed" })
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe(`/api/research/desktop/operations/${pending.requestId}?generation=${generation}`)
    expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0)
    act(() => history.acknowledgeAccountHistory(5, history.historyState().generation))
    first.unmount(); sessionStorage.clear()
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={onAttach} />)
    fireEvent.click(await screen.findByRole("button", { name: "用于本次研究" }))
    expect(onAttach).toHaveBeenCalledWith("import-owned")
    expect(history.historyState().workspace.materials?.[0]).toMatchObject({ id: "import-owned", kind: "pdf", referenceId: "paper-owned", readerHref: `/papers/paper-owned/read?sourceRevision=${encodeURIComponent(sourceRevision)}` })
    expect(screen.getByRole("button", { name: "入库待保存" })).toBeDisabled()
    act(() => history.acknowledgeAccountHistory(6, history.historyState().generation))
    await waitFor(() => expect(screen.getByRole("button", { name: "已加入本次研究" })).toBeDisabled())
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it("does not bind a passed receipt with the wrong request or source identity", async () => {
    vi.mocked(fetch).mockResolvedValue(response({ ...receipt, result: { ...receipt.result, sourceFingerprint: `sha256:${"d".repeat(64)}` } }))
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={vi.fn()} />)
    fireEvent.click(await screen.findByRole("button", { name: "检查已有请求" }))
    expect(await screen.findByRole("alert")).toHaveTextContent(/paper\/source identity/)
    expect(history.historyState().workspace.pendingOperations?.[0]?.status).toBe("unknown")
    expect(screen.queryByRole("button", { name: "用于本次研究" })).not.toBeInTheDocument()
    expect((history.historyState().workspace.materials ?? [])).toEqual([])
  })

  it("does not adopt output identity from a recheck that is no longer passed", async () => {
    vi.mocked(fetch).mockResolvedValue(response({ ...receipt, status: "needs_action" }))
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={vi.fn()} />)
    fireEvent.click(await screen.findByRole("button", { name: "检查已有请求" }))
    await screen.findByText("结果需要核对")
    const downgraded = history.historyState().workspace.pendingOperations?.[0]
    expect(downgraded).toMatchObject({ status: "needs_action", sourceRevision })
    expect(downgraded).not.toHaveProperty("paperId")
    expect(downgraded).not.toHaveProperty("importId")
    expect(screen.queryByRole("button", { name: "用于本次研究" })).not.toBeInTheDocument()
  })

  it("deduplicates by the canonical paper and preserves user library metadata", async () => {
    load(completed)
    history.acceptAccountHistory({ ...history.historyState().workspace, materials: [{ id: "existing-row", kind: "pdf", referenceId: "paper-owned", groupId: null, title: "My title", notes: "My notes", url: "", favorite: true, readingStatus: "reading", tags: ["keep"], readerHref: `/papers/paper-owned/read?sourceRevision=${encodeURIComponent(sourceRevision)}`, createdAt: 1, updatedAt: 2 }] }, 5)
    const onAttach = vi.fn()
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={onAttach} />)
    fireEvent.click(await screen.findByRole("button", { name: "用于本次研究" }))
    expect(history.historyState().workspace.materials).toHaveLength(1)
    expect(history.historyState().workspace.materials?.[0]).toMatchObject({ id: "existing-row", title: "My title", notes: "My notes", favorite: true, tags: ["keep"], readingStatus: "reading" })
    expect(onAttach).toHaveBeenCalledWith("existing-row")
  })

  it("retains the completed receipt when library enqueue fails so the same result can be retried", async () => {
    load(completed)
    const save = vi.spyOn(history, "saveResearchMaterial").mockReturnValueOnce(false)
    const onAttach = vi.fn()
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={onAttach} />)
    fireEvent.click(await screen.findByRole("button", { name: "用于本次研究" }))
    expect(onAttach).not.toHaveBeenCalled()
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法引用 PDF")
    expect(screen.getByRole("button", { name: "用于本次研究" })).toBeEnabled()
    save.mockRestore()
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(onAttach).toHaveBeenCalledWith("import-owned")
    expect(fetch).not.toHaveBeenCalled()
  })

  it("does not adopt a late previous-owner status result", async () => {
    let finish: (value: Response) => void = () => {}
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(resolve => { finish = resolve }))
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={vi.fn()} />)
    fireEvent.click(await screen.findByRole("button", { name: "检查已有请求" }))
    act(() => { history.selectHistoryOwner("new-owner"); history.acceptAccountHistory(emptyWorkspace(), 0) })
    await act(async () => { finish(response(receipt)) })
    expect(history.historyState().workspace.pendingOperations ?? []).toEqual([])
    expect(history.historyState().workspace.materials ?? []).toEqual([])
    expect(screen.queryByText("durable.pdf")).not.toBeInTheDocument()
  })
})

const identity = {
  owner: { userId: "owner-recovery", tenantId: null, memoryNamespace: "research:user:owner-recovery" },
  generation,
  requestContract: { fingerprint: "sha256-canonical-json-v1", requestId: "stable request identity", notFoundRetry: "query original request" },
}
const sourceUrl = "https://doi.org/10.1234/example"
const knownSource: ResearchMaterial = { id: "known-source-row", groupId: null, kind: "paper", referenceId: "paper-source", title: "A real identified source title", url: sourceUrl, notes: "", createdAt: 1, updatedAt: 2 }
const preparedSource: ResearchPendingOperation = {
  requestId: "request-durable-source", generation, fingerprint, kind: "prepare",
  semantic: { kind: "prepare", paperId: "paper-source", sourceUrl, sourceType: null },
  status: "passed", paperId: "paper-source", sourceRevision: "source-revision-1", sourceFingerprint: sourceRevision,
  createdAt: 1, updatedAt: 3,
}

describe("direct source admission", () => {
  it("normalizes a DOI without treating it as a paper identity and rejects unsafe transports", () => {
    expect(normalizeDirectSource(" doi:10.1234/example ")).toBe(sourceUrl)
    expect(normalizeDirectSource("https://example.org/paper#methods")).toBe("https://example.org/paper")
    for (const value of ["/papers/one", "file:///paper.pdf", "https://name:secret@example.org/paper", "http://localhost/paper", "http://[::1]/paper", "http://127.0.0.1/paper"]) {
      expect(normalizeDirectSource(value)).toBeNull()
    }
  })

  it("persists admission identity before prepare, accepts only a real passed source receipt, then saves the material", async () => {
    history.acceptAccountHistory({ ...emptyWorkspace(), materials: [knownSource] }, 4)
    vi.mocked(fetch).mockImplementation(async (url, init) => {
      if (String(url) === "/api/research/desktop/identity") return response(identity)
      if (String(url) === "/api/research/desktop/operations/prepare") {
        const body = JSON.parse(String(init?.body))
        const saved = history.historyState().workspace.pendingOperations?.[0]
        expect(saved).toMatchObject({ requestId: body.requestId, fingerprint: body.requestFingerprint, semantic: { paperId: "paper-source", sourceUrl } })
        return response({ requestId: body.requestId, generation: body.generation, fingerprint: body.requestFingerprint, kind: "prepare", status: "passed", paperId: "paper-source", sourceRevision: "source-revision-1", result: { status: "ready", paperId: "paper-source", sourceRevision: "source-revision-1", sourceFingerprint: sourceRevision }, recoverable: false, automaticRetryAllowed: false })
      }
      throw new Error("Unexpected request: " + String(url))
    })
    const onAttach = vi.fn()
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={onAttach} />)
    fireEvent.change(await screen.findByRole("textbox", { name: "论文来源 URL 或 DOI" }), { target: { value: "10.1234/example" } })
    fireEvent.change(screen.getByRole("combobox", { name: "选择已入库的目标论文" }), { target: { value: "paper-source" } })
    fireEvent.click(screen.getByRole("button", { name: "准备所选论文来源" }))
    await waitFor(() => expect(history.historyState().workspace.pendingOperations).toHaveLength(1))
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(onAttach).not.toHaveBeenCalled()
    expect(history.historyState().dirty).toBe(true)
    act(() => history.acknowledgeAccountHistory(5, history.historyState().generation))
    await screen.findByText("文本已转换，可用于本次研究")
    const posts = vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST")
    expect(posts).toHaveLength(1)
    expect(posts[0][0]).toBe("/api/research/desktop/operations/prepare")
    expect(JSON.parse(String(posts[0][1]?.body))).toMatchObject({ paperId: "paper-source", sourceUrl, sourceType: null })
    expect(history.historyState().workspace.pendingOperations?.[0]).toMatchObject({ status: "passed", paperId: "paper-source", sourceRevision: "source-revision-1", sourceFingerprint: sourceRevision })
    expect(history.historyState().workspace.materials).toEqual([knownSource])
    fireEvent.click(screen.getByRole("button", { name: "用于本次研究" }))
    expect(history.historyState().workspace.materials?.[0]).toMatchObject({ kind: "paper", referenceId: "paper-source", url: sourceUrl })
    expect(onAttach).toHaveBeenCalledOnce()
    expect(screen.getByRole("button", { name: "入库待保存" })).toBeDisabled()
  })

  it("reopens a prepared source from durable identity without network or an import id", async () => {
    load(preparedSource)
    const onAttach = vi.fn()
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={onAttach} />)
    fireEvent.click(await screen.findByRole("button", { name: "用于本次研究" }))
    expect(onAttach).toHaveBeenCalledWith("source:request-durable-source")
    expect(history.historyState().workspace.materials?.[0]?.referenceId).toBe("paper-source")
    expect(fetch).not.toHaveBeenCalled()
  })

  it("retains an uncertain direct source for explicit GET and never resubmits it", async () => {
    load({ ...preparedSource, status: "unknown", sourceRevision: undefined, sourceFingerprint: undefined })
    history.acceptAccountHistory({ ...history.historyState().workspace, materials: [knownSource] }, 4)
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={vi.fn()} />)
    expect(await screen.findByRole("button", { name: "检查已有请求" })).toBeEnabled()
    fireEvent.change(screen.getByRole("textbox", { name: "论文来源 URL 或 DOI" }), { target: { value: sourceUrl } })
    fireEvent.change(screen.getByRole("combobox", { name: "选择已入库的目标论文" }), { target: { value: "paper-source" } })
    fireEvent.click(screen.getByRole("button", { name: "准备所选论文来源" }))
    expect(await screen.findByRole("alert")).toHaveTextContent("请检查原请求")
    expect(fetch).not.toHaveBeenCalled()
    expect((history.historyState().workspace.materials ?? [])).toEqual([knownSource])
  })

  it("does not promote an incomplete restored passed receipt into a readable source", async () => {
    load({ ...preparedSource, sourceFingerprint: undefined })
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={vi.fn()} />)
    expect(await screen.findByRole("button", { name: "检查已有请求" })).toBeEnabled()
    expect(screen.queryByRole("button", { name: "用于本次研究" })).not.toBeInTheDocument()
    expect(fetch).not.toHaveBeenCalled()
  })

  it("does not send or queue a prepare while offline", async () => {
    history.acceptAccountHistory({ ...emptyWorkspace(), materials: [knownSource] }, 4)
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(false)
    render(<ResearchSourceEntry appearance="paper-agent" onAttach={vi.fn()} />)
    fireEvent.change(await screen.findByRole("textbox", { name: "论文来源 URL 或 DOI" }), { target: { value: sourceUrl } })
    fireEvent.change(screen.getByRole("combobox", { name: "选择已入库的目标论文" }), { target: { value: "paper-source" } })
    expect(screen.getByRole("button", { name: "准备所选论文来源" })).toBeDisabled()
    fireEvent(window, new Event("online"))
    expect(fetch).not.toHaveBeenCalled()
    expect(history.historyState().workspace.pendingOperations ?? []).toEqual([])
  })
})

it("requires a real identified library target and explains the online discovery path instead of asking for an internal id", async () => {
  history.acceptAccountHistory(emptyWorkspace(), 4)
  render(<ResearchSourceEntry appearance="paper-agent" onAttach={vi.fn()} />)
  const target = await screen.findByRole("combobox", { name: "选择已入库的目标论文" })
  expect(target).toBeDisabled()
  expect(screen.getByRole("button", { name: "准备所选论文来源" })).toBeDisabled()
  expect(screen.queryByRole("textbox", { name: "来源对应的论文 ID" })).not.toBeInTheDocument()
  expect(screen.getByText(/当前库中没有可绑定的已识别论文/)).toBeVisible()
  expect(screen.getByText(/不确定或未知的 arXiv/)).toHaveTextContent("选择真实识别结果并保存到库")
  expect(fetch).not.toHaveBeenCalled()
})

it("shows the current owner library title and canonical id and excludes unrecognized or non-paper targets", async () => {
  history.acceptAccountHistory({ ...emptyWorkspace(), materials: [knownSource, { ...knownSource, id: "unrecognized", referenceId: undefined, title: "No canonical identity" }, { ...knownSource, id: "not-a-paper", kind: "project", referenceId: "project-canonical", title: "Other kind" }] }, 4)
  render(<ResearchSourceEntry appearance="paper-agent" onAttach={vi.fn()} />)
  const target = await screen.findByRole("combobox", { name: "选择已入库的目标论文" })
  expect(screen.getByRole("option", { name: "A real identified source title — paper-source" })).toBeVisible()
  expect(screen.queryByRole("option", { name: /No canonical identity/ })).not.toBeInTheDocument()
  expect(screen.queryByRole("option", { name: /Other kind/ })).not.toBeInTheDocument()
  fireEvent.change(target, { target: { value: "paper-source" } })
  expect(screen.getByText(/已识别目标/)).toHaveTextContent("A real identified source title（真实 ID：paper-source）")
  act(() => { history.selectHistoryOwner("other-target-owner"); history.acceptAccountHistory(emptyWorkspace(), 0) })
  expect(await screen.findByRole("combobox", { name: "选择已入库的目标论文" })).toBeDisabled()
  expect(screen.queryByRole("option", { name: /A real identified source title/ })).not.toBeInTheDocument()
  expect(fetch).not.toHaveBeenCalled()
})

it("does not submit when the chosen canonical library target is removed before identity is ready", async () => {
  history.acceptAccountHistory({ ...emptyWorkspace(), materials: [knownSource] }, 4)
  let finish: (value: Response) => void = () => {}
  vi.mocked(fetch).mockImplementation(() => new Promise<Response>(resolve => { finish = resolve }))
  render(<ResearchSourceEntry appearance="paper-agent" onAttach={vi.fn()} />)
  fireEvent.change(await screen.findByRole("textbox", { name: "论文来源 URL 或 DOI" }), { target: { value: sourceUrl } })
  fireEvent.change(screen.getByRole("combobox", { name: "选择已入库的目标论文" }), { target: { value: "paper-source" } })
  fireEvent.click(screen.getByRole("button", { name: "准备所选论文来源" }))
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
  act(() => history.acceptAccountHistory(emptyWorkspace(), 5))
  await act(async () => { finish(response(identity)) })
  expect(await screen.findByRole("alert")).toHaveTextContent("目标论文已不在当前库中")
  expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0)
  expect(history.historyState().workspace.pendingOperations ?? []).toEqual([])
})
