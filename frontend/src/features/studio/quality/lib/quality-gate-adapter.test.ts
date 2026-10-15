import { describe, expect, it } from "vitest"
import {
  buildQualityDashboard,
  buildQualityDetail,
  mapQualityChecks,
  statusFromDecision,
  type ApiReportQuality
} from "@/features/studio/quality/lib/quality-gate-adapter"

const blockedQuality: ApiReportQuality = {
  report_id: "report-1",
  run_id: "run-1",
  status: "final",
  quality_score: 0.64,
  quality: {
    decision: "blocked",
    route: "human_review",
    citation_coverage: 0.72,
    source_freshness: 0.86,
    duplicate_rate: 0.09,
    citation_failure_categories: [{ code: "unsupported_claims", count: 2 }],
    unsupported_claims: ["Claim without supporting evidence"],
    unsupported_sections: ["Market impact"],
    quality_lineage: {
      claim_count: 4,
      quality_result_count: 1,
      rejecting_evidence_ids: ["ev-2"]
    },
    reviewer_trace: {
      approval_id: "approval-1",
      artifact_refs: {
        report_quality_summary: "report_quality_summary.json"
      }
    }
  }
}

describe("quality-gate-adapter", () => {
  it("maps report quality payloads into actionable checks", () => {
    const checks = mapQualityChecks(blockedQuality)

    expect(checks.map((check) => check.id)).toEqual(
      expect.arrayContaining(["quality-decision", "citation-coverage", "unsupported-claims", "reviewer-trace"])
    )
    expect(checks.find((check) => check.id === "quality-decision")?.status).toBe("review_required")
    expect(checks.find((check) => check.id === "unsupported-claims")?.userActionRequired).toBe(true)
  })

  it("returns unknown state when quality is missing", () => {
    const checks = mapQualityChecks(undefined)

    expect(checks).toEqual([
      expect.objectContaining({
        id: "quality-unknown",
        status: "unknown"
      })
    ])
  })

  it("maps blocked and human-review decisions to review_required", () => {
    expect(statusFromDecision("blocked", 0.95)).toBe("review_required")
    expect(statusFromDecision("human_review", 0.95)).toBe("review_required")
  })

  it("aggregates dashboard counts and quality metrics", () => {
    const dashboard = buildQualityDashboard({
      reports: {
        reports: [
          { report_id: "report-1", run_id: "run-1", title: "Blocked report", quality_score: 0.64 },
          { report_id: "report-2", run_id: "run-2", title: "Passed report", quality_score: 0.95 }
        ]
      },
      qualities: [
        blockedQuality,
        {
          report_id: "report-2",
          run_id: "run-2",
          status: "final",
          quality_score: 0.95,
          quality: {
            decision: "passed",
            citation_coverage: 0.94,
            source_freshness: 0.96,
            duplicate_rate: 0.02,
            unsupported_claims: []
          }
        }
      ],
      runHealth: [{ run_id: "run-1", health: { healthy: false, severity: "warning", summary: "blocked" } }],
      diagnostics: [{ run_id: "run-1", diagnostics: { healthy: false, timeline_summary: { event_count: 7 } } }]
    })

    expect(dashboard.dataState).toBe("ready")
    expect(dashboard.counts.review_required).toBe(1)
    expect(dashboard.counts.passed).toBe(1)
    expect(dashboard.metrics.citationCoverage).toBe(83)
    expect(dashboard.metrics.sourceFreshness).toBe(91)
    expect(dashboard.metrics.duplicateRate).toBe(6)
    expect(dashboard.metrics.unsupportedClaims).toBe(2)
    expect(dashboard.recentBlockedRuns[0]).toMatchObject({ runId: "run-1", latestEventCount: 7 })
  })

  it("builds detail view with linked artifacts and failure reasons", () => {
    const detail = buildQualityDetail({
      reportId: "report-1",
      report: { report_id: "report-1", run_id: "run-1", title: "Blocked report" },
      quality: blockedQuality
    })

    expect(detail.dataState).toBe("ready")
    expect(detail.report.status).toBe("review_required")
    expect(detail.artifactRefs).toContainEqual({ label: "report_quality_summary", value: "report_quality_summary.json" })
    expect(detail.failureReasons.length).toBeGreaterThan(0)
  })
})
