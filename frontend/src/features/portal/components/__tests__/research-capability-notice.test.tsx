import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { act, cleanup, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { notifyModelConnectionsChanged } from "@/lib/account/model-connection-events"
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
const modelBacked = (reasonCode: string, message = "", id = "paper-question-answering") => ({ id, label: "论文问答", required: true, configured: false, reachable: false, verified: false, verifiedAt: null, reasonCode, message, provenance: null })
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

  it("shows the purpose each blocked capability lacks and offers one fix for all of them", async () => {
    mocks.fetchDesktopCapabilities.mockResolvedValue(capabilities(
      verifiedSource,
      modelBacked("model_connection_required", "请先在设置里为“候选生成”配置可用的模型连接。", "paper-analysis"),
      modelBacked("model_connection_required", "请先在设置里为“回答”配置可用的模型连接。"),
    ))
    renderNotice()
    expect(await screen.findByText(
      "请先在设置里为“候选生成”配置可用的模型连接。 请先在设置里为“回答”配置可用的模型连接。",
    )).toBeInTheDocument()
    expect(screen.getAllByRole("button", { name: "设置模型连接" })).toHaveLength(1)
    expect(screen.getByText("2 项必需能力尚未验证")).toBeInTheDocument()
  })

  it("asks for the key again after the server key rotated", async () => {
    mocks.fetchDesktopCapabilities.mockResolvedValue(capabilities(
      modelBacked("model_connection_key_rotated", "服务的加密密钥已更换，请在设置里重新填写“回答”所用的模型连接的 API Key。"),
    ))
    renderNotice()
    expect(await screen.findByText("服务的加密密钥已更换，请在设置里重新填写“回答”所用的模型连接的 API Key。")).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "重新填写 API Key" })).toBeInTheDocument()
  })

  it("falls back to a general prompt when the gap carries no message", async () => {
    mocks.fetchDesktopCapabilities.mockResolvedValue(capabilities(modelBacked("model_connection_window_too_small")))
    renderNotice()
    expect(await screen.findByText("分析与问答需要你自己的模型连接。")).toBeInTheDocument()
  })

  it("shows no connection prompt for gaps the connection settings do not fix", async () => {
    mocks.fetchDesktopCapabilities.mockResolvedValue(capabilities(modelBacked("verification_unknown")))
    renderNotice()
    expect(await screen.findByText("1 项必需能力尚未验证")).toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /模型连接|API Key/ })).not.toBeInTheDocument()
  })

  it("re-reads capabilities after the account's connections change", async () => {
    mocks.fetchDesktopCapabilities
      .mockResolvedValueOnce(capabilities(modelBacked("model_connection_required")))
      .mockResolvedValueOnce(capabilities(verifiedSource))
    renderNotice()
    await screen.findByText("分析与问答需要你自己的模型连接。")

    act(() => notifyModelConnectionsChanged())

    await waitFor(() => expect(screen.queryByText("分析与问答需要你自己的模型连接。")).not.toBeInTheDocument())
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
