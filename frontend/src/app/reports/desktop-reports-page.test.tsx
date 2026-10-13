import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"
import { fetchSavedResearchReports } from "@/lib/research/desktop/client"
import { DesktopReportsPage } from "./desktop-reports-page"

const mocks = vi.hoisted(() => ({
  owner: "alice" as string | null,
  workspace: {
    reportDrafts: [{
      id: "draft-1",
      question: "保留的问题",
      title: "保留的准备稿",
      scope: "",
      notes: "网络失败时也不能丢失",
      materials: [],
      updatedAt: 1,
    }],
  },
}))

vi.mock("@/lib/research/use-research-history", () => ({
  useOwnedResearchWorkspace: () => ({
    owner: mocks.owner,
    ready: true,
    workspace: mocks.workspace,
  }),
}))
vi.mock("@/lib/research/desktop/client", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/research/desktop/client")>()
  return {
    ...original,
    fetchSavedResearchReports: vi.fn(),
    exportResearchDraft: vi.fn(),
  }
})

describe("DesktopReportsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.owner = "alice"
  })

  it("retries a failed owned report listing without dropping the current draft", async () => {
    vi.mocked(fetchSavedResearchReports)
      .mockRejectedValueOnce(new Error("报告目录暂时不可用。"))
      .mockResolvedValueOnce([{
        reportId: "report-1",
        runId: "run-1",
        paperId: "2401.01234",
        sourceRevision: "revision-1",
        title: "已接受报告",
        savedAt: "2026-10-12T00:00:00Z",
        updatedAt: "2026-10-12T00:00:00Z",
        metadata: {},
      }])

    render(<DesktopReportsPage />)

    expect(await screen.findByRole("alert")).toHaveTextContent("报告目录暂时不可用。")
    expect(screen.getByText("保留的准备稿")).toBeVisible()
    fireEvent.click(screen.getByRole("button", { name: "重新读取报告目录" }))

    expect(await screen.findByRole("link", { name: "已接受报告" })).toBeVisible()
    expect(screen.getByText("保留的准备稿")).toBeVisible()
    await waitFor(() => expect(fetchSavedResearchReports).toHaveBeenCalledTimes(2))
  })

  it("clears an old owner report list when the owner key changes", async () => {
    vi.mocked(fetchSavedResearchReports)
      .mockResolvedValueOnce([{
        reportId: "alice-report",
        runId: "alice-run",
        paperId: "2401.01234",
        sourceRevision: "alice-revision",
        title: "Alice 报告",
        savedAt: "2026-10-12T00:00:00Z",
        updatedAt: "2026-10-12T00:00:00Z",
        metadata: {},
      }])
      .mockResolvedValueOnce([])

    const view = render(<DesktopReportsPage />)
    expect(await screen.findByRole("link", { name: "Alice 报告" })).toBeVisible()

    mocks.owner = "bob"
    view.rerender(<DesktopReportsPage />)
    await waitFor(() => expect(screen.queryByRole("link", { name: "Alice 报告" })).not.toBeInTheDocument())
    expect(fetchSavedResearchReports).toHaveBeenCalledTimes(2)
  })
})
