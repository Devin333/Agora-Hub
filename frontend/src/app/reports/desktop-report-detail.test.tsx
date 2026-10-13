import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { DesktopSavedReportDetail } from "./desktop-report-detail"
import {
  downloadResearchReport,
  fetchSavedResearchReport,
} from "@/lib/research/desktop/client"
import type { ResearchReport } from "@/lib/research/desktop/contracts"

vi.mock("@/lib/research/use-research-history", () => ({
  useOwnedResearchWorkspace: () => ({
    ready: true,
    owner: "owner-1",
    workspace: {},
  }),
}))

vi.mock("@/lib/research/desktop/client", async importOriginal => {
  const original = await importOriginal<typeof import("@/lib/research/desktop/client")>()
  return {
    ...original,
    fetchSavedResearchReport: vi.fn(),
    downloadResearchReport: vi.fn(),
  }
})

describe("DesktopSavedReportDetail", () => {
  beforeEach(() => {
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:report"),
      revokeObjectURL: vi.fn(),
    })
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined)
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.unstubAllGlobals()
  })

  it("renders only backend-verified citation locators and accepted export", async () => {
    vi.mocked(fetchSavedResearchReport).mockResolvedValue(savedReport(reportFixture()))
    render(<DesktopSavedReportDetail reportId="report-1" />)

    expect(await screen.findByRole("heading", { name: "Verified report" })).toBeVisible()
    expect(screen.getByRole("link", { name: "回到论文定位" })).toHaveAttribute(
      "href",
      "/papers/2401.01234/read?sourceRevision=revision-1&resumeSection=methods&resumePage=3",
    )
    expect(screen.getByRole("button", { name: "导出 Markdown" })).toBeVisible()
  })

  it("keeps a corrupt report readable but removes accepted export actions", async () => {
    vi.mocked(fetchSavedResearchReport).mockResolvedValue(
      savedReport(
        reportFixture({
          diagnostics: {
            artifactStatus: "corrupt",
            artifactReason: "artifact checksum mismatch",
            reportId: "report-1",
            title: "Verified report",
          },
          eligibility: {
            accepted: true,
            qualityPassed: true,
            acceptedExport: false,
          },
        }),
      ),
    )
    render(<DesktopSavedReportDetail reportId="report-1" />)

    expect(await screen.findByRole("heading", { name: "Verified report" })).toBeVisible()
    expect(screen.queryByRole("button", { name: "导出 Markdown" })).not.toBeInTheDocument()
    expect(screen.getByText("报告产物已损坏，当前不可导出")).toBeVisible()
  })

  it("retries an initial saved-report load failure", async () => {
    vi.mocked(fetchSavedResearchReport)
      .mockRejectedValueOnce(new Error("报告服务暂时不可用。"))
      .mockResolvedValueOnce(savedReport(reportFixture()))
    render(<DesktopSavedReportDetail reportId="report-1" />)

    expect(await screen.findByRole("alert")).toHaveTextContent("报告服务暂时不可用。")
    fireEvent.click(screen.getByRole("button", { name: "重新读取报告" }))
    expect(await screen.findByRole("heading", { name: "Verified report" })).toBeVisible()
    expect(fetchSavedResearchReport).toHaveBeenCalledTimes(2)
  })

  it("reports an authenticated export failure and permits a retry", async () => {
    vi.mocked(fetchSavedResearchReport).mockResolvedValue(savedReport(reportFixture()))
    vi.mocked(downloadResearchReport)
      .mockRejectedValueOnce(new Error("登录状态已失效。"))
      .mockResolvedValueOnce({
        blob: new Blob(["report"]),
        filename: "report.md",
      })
    render(<DesktopSavedReportDetail reportId="report-1" />)

    const button = await screen.findByRole("button", { name: "导出 Markdown" })
    fireEvent.click(button)
    expect(await screen.findByRole("alert")).toHaveTextContent("登录状态已失效。")

    fireEvent.click(button)
    expect(await screen.findByRole("status")).toHaveTextContent("Markdown 已下载。")
    await waitFor(() => expect(downloadResearchReport).toHaveBeenCalledTimes(2))
  })
})

function savedReport(report: ResearchReport) {
  return {
    reference: {
      reportId: "report-1",
      runId: "run-1",
      paperId: "2401.01234",
      sourceRevision: "revision-1",
      title: "Verified report",
      savedAt: "2026-10-12T00:00:00Z",
      updatedAt: "2026-10-12T00:00:00Z",
      metadata: { accepted: true },
    },
    report,
  }
}

function analysisFixture(): NonNullable<ResearchReport["analysis"]> {
  return {
    paper_id: "2401.01234",
    summary: {
      problem: "The paper studies a verified research problem.",
      core_idea: "It uses an evidence-bound method.",
      key_contributions: ["Contribution one"],
      method_summary: "Method summary",
      experiment_summary: "Experiment summary",
      limitations: ["Known limitation"],
      why_it_matters: "Why it matters",
      read_next: ["Read the methods section"],
      evidenceIds: [],
      confidence: 0.9,
      quality_flags: [],
    },
    contributions: ["Contribution one"],
    methods: ["Method detail"],
    experiments: ["Experiment detail"],
    limitations: ["Known limitation"],
    reproducibility: ["Reproducibility note"],
    related_work: [],
    claims: [
      {
        claim_id: "claim-1",
        text: "Evidence-bound claim",
        claim_type: "method" as const,
        section_id: "methods",
        evidence_ids: ["evidence-1"],
        confidence: 0.9,
        },
    ],
    evidence_pack_id: "pack-1",
    quality: {},
  }
}

function qualityFixture(): NonNullable<ResearchReport["quality"]> {
  return {
    result_id: "quality-1",
    target_id: "2401.01234",
    target_type: "reader_payload",
    passed: true,
    score: 1,
    gate_results: [
      {
        gate_name: "evidence",
        passed: true,
        reasons: [],
        quality_flags: [],
        },
    ],
    quality_flags: [],
  }
}

function reportFixture(overrides: Partial<ResearchReport> = {}): ResearchReport {
  return {
    schema: "newsroom.research-report/v1",
    runId: "run-1",
    paperId: "2401.01234",
    sourceRevision: "revision-1",
    status: "succeeded",
    disposition: "accepted",
    analysis: analysisFixture(),
    quality: qualityFixture(),
    citations: [
      {
        id: "citation-1",
        label: "Methods",
        paperId: "2401.01234",
        sourceRevision: "revision-1",
        valid: true,
        sectionId: "methods",
        page: 3,
      },
    ],
    artifactRefs: [],
    traceRef: null,
    diagnostics: {
      artifactStatus: "valid",
      artifactReason: "",
      reportId: "report-1",
      title: "Verified report",
    },
    eligibility: {
      accepted: true,
      qualityPassed: true,
      acceptedExport: true,
    },
    ...overrides,
  }
}
