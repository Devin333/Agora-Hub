import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { notifyModelCredentialChanged } from "@/lib/account/model-credential-events"
import type { DesktopCapabilities } from "@/lib/research/desktop/contracts"
import { ResearchCapabilityNotice } from "../research-capability-notice"

const mocks = vi.hoisted(() => ({
  fetchDesktopCapabilities: vi.fn<(signal?: AbortSignal) => Promise<DesktopCapabilities>>(),
  workspace: { owner: "alice" as string | null, ready: true },
}))
vi.mock("@/lib/research/desktop/client", () => ({ fetchDesktopCapabilities: mocks.fetchDesktopCapabilities }))
vi.mock("@/lib/research/use-research-history", () => ({ useOwnedResearchWorkspace: () => mocks.workspace }))

const generation = `gen_${"1".repeat(32)}`
const verifiedSource = { id: "source", label: "论文来源", required: true, configured: true, reachable: true, verified: true, verifiedAt: "2026-10-27T08:00:00Z", reasonCode: "business_operation_verified", message: "", provenance: null }
const modelBacked = (reasonCode: string) => ({ id: "paper-question-answering", label: "论文问答", required: true, configured: false, reachable: false, verified: false, verifiedAt: null, reasonCode, message: "", provenance: null })
const capabilities = (...items: object[]) => ({ generation, capabilities: items }) as DesktopCapabilities

function renderNotice() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><ResearchCapabilityNotice /></QueryClientProvider>)
}

describe("ResearchCapabilityNotice", () => {
  beforeEach(() => {
    vi.stubEnv("NEWSROOM_DEPLOYMENT_PROFILE", "hosted")
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", "hosted")
    mocks.workspace = { owner: "alice", ready: true }
    mocks.fetchDesktopCapabilities.mockReset()
  })
  afterEach(() => { cleanup(); vi.unstubAllEnvs() })

  it("offers one fix for every capability blocked by the missing account key", async () => {
    mocks.fetchDesktopCapabilities.mockResolvedValue(capabilities(verifiedSource, modelBacked("model_credential_required")))
    renderNotice()
    expect(await screen.findByText("分析与问答需要你自己的模型 API Key。")).toBeInTheDocument()
    expect(screen.getAllByRole("button", { name: "填写模型 API Key" })).toHaveLength(1)
    expect(screen.getByText("1 项必需能力尚未验证")).toBeInTheDocument()
  })

  it("asks for the key again after the server key rotated", async () => {
    mocks.fetchDesktopCapabilities.mockResolvedValue(capabilities(modelBacked("model_credential_key_rotated")))
    renderNotice()
    expect(await screen.findByText("保存的模型 API Key 需要重新填写。")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "重新填写模型 API Key" })).toBeInTheDocument()
  })

  it("shows no key prompt for gaps a key does not fix", async () => {
    mocks.fetchDesktopCapabilities.mockResolvedValue(capabilities(modelBacked("verification_unknown")))
    renderNotice()
    expect(await screen.findByText("1 项必需能力尚未验证")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /模型 API Key/ })).not.toBeInTheDocument()
  })

  it("re-reads capabilities after the account key changes", async () => {
    mocks.fetchDesktopCapabilities
      .mockResolvedValueOnce(capabilities(modelBacked("model_credential_required")))
      .mockResolvedValueOnce(capabilities(verifiedSource))
    renderNotice()
    await screen.findByText("分析与问答需要你自己的模型 API Key。")

    act(() => notifyModelCredentialChanged())

    await waitFor(() => expect(screen.queryByText("分析与问答需要你自己的模型 API Key。")).not.toBeInTheDocument())
    expect(mocks.fetchDesktopCapabilities).toHaveBeenCalledTimes(2)
    expect(screen.getByText("所需能力已经验证")).toBeInTheDocument()
  })

  it("renders nothing outside the hosted profile", () => {
    vi.stubEnv("NEWSROOM_DEPLOYMENT_PROFILE", "default")
    vi.stubEnv("NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE", "default")
    const { container } = renderNotice()
    expect(container).toBeEmptyDOMElement()
    expect(mocks.fetchDesktopCapabilities).not.toHaveBeenCalled()
  })
})
