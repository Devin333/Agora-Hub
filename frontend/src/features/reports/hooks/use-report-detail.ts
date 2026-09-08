import { useCallback, useEffect, useState } from "react";
import type { Evidence } from "@/types/evidence";
import type { MockHookResult } from "@/types/common";
import type { NewsItem } from "@/types/news";
import type { Report, ReportStatus, ReportType } from "@/types/report";
import type { Topic } from "@/types/topic";

export type ReportDetailData = {
  report?: Report;
  relatedTopics: Topic[];
  relatedNews: NewsItem[];
  evidence: Evidence[];
};

export function useReportDetail(id: string): MockHookResult<ReportDetailData> {
  const [apiReport, setApiReport] = useState<Report | undefined>();
  const [error, setError] = useState<Error | undefined>();
  const [isLoading, setIsLoading] = useState(true);

  const loadReport = useCallback(async () => {
    setIsLoading(true);
    try {
      setApiReport(await fetchReportDetail(id));
      setError(undefined);
    } catch (caught) {
      setApiReport(undefined);
      setError(caught instanceof Error ? caught : new Error("Failed to load report"));
    } finally {
      setIsLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void loadReport();
  }, [loadReport]);

  const report = apiReport;
  return {
    data: {
      report,
      relatedTopics: [],
      relatedNews: [],
      evidence: [],
    },
    isLoading: isLoading && apiReport === undefined,
    isError: Boolean(error) && apiReport === undefined && report === undefined,
    error,
    refetch: () => {
      void loadReport();
    },
  };
}

type ApiEnvelope<T> = {
  success?: boolean;
  data?: T | null;
  error?: { message?: string | null } | null;
};

type ApiReportDetail = {
  report_id?: string | null;
  id?: string | null;
  run_id?: string | null;
  title?: string | null;
  status?: string | null;
  summary?: string | null;
  created_at?: string | null;
  published_at?: string | null;
  quality_score?: number | null;
  report_markdown?: string | null;
  report_json?: {
    title?: string | null;
    sections?: Array<{
      title?: string | null;
      content?: string | null;
      sources?: string[] | null;
      claim_grounding?: Array<{ claim_id?: string | null }> | null;
    }> | null;
    source_urls?: string[] | null;
    metadata?: Record<string, unknown> | null;
  } | null;
  metadata?: Record<string, unknown> | null;
};

async function fetchReportDetail(id: string): Promise<Report> {
  const decodedId = safeDecodeURIComponent(id);
  const response = await fetch(`/api/v1/reports/${encodeURIComponent(decodedId)}`, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  const payload = await response.json() as ApiEnvelope<ApiReportDetail> | ApiReportDetail;
  if (!response.ok) {
    throw new Error(`Report API failed: ${response.status}`);
  }
  return mapReportDetail(unwrapEnvelope(payload));
}

function safeDecodeURIComponent(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function unwrapEnvelope<T>(payload: ApiEnvelope<T> | T): T {
  if (typeof payload === "object" && payload !== null && "success" in payload) {
    if (payload.success === false) {
      throw new Error(payload.error?.message ?? "Report API returned an error");
    }
    return (payload.data ?? {}) as T;
  }
  return payload as T;
}

function mapReportDetail(report: ApiReportDetail): Report {
  const generatedAt = report.published_at ?? report.created_at ?? new Date(0).toISOString();
  const reportType = mapReportType(report);
  const evidenceIds = new Set<string>();
  for (const section of report.report_json?.sections ?? []) {
    for (const claim of section.claim_grounding ?? []) {
      if (claim.claim_id) evidenceIds.add(claim.claim_id);
    }
  }
  return {
    id: report.report_id ?? report.id ?? report.run_id ?? "unknown-report",
    title: report.title ?? report.report_json?.title ?? report.report_id ?? "Untitled report",
    type: reportType,
    reportType,
    markdown: report.report_markdown ?? buildMarkdown(report),
    generatedAt,
    coveredFrom: generatedAt,
    coveredTo: generatedAt,
    agentName: "Agora Hub Runtime",
    qualityScore: typeof report.quality_score === "number" ? report.quality_score : undefined,
    topicIds: [],
    newsItemIds: [],
    evidenceIds: Array.from(evidenceIds),
    status: mapReportStatus(report.status),
  };
}

function buildMarkdown(report: ApiReportDetail): string {
  const title = report.title ?? report.report_json?.title ?? "Untitled report";
  const sections = report.report_json?.sections ?? [];
  const body = sections
    .map((section) => `## ${section.title ?? "Section"}\n${section.content ?? ""}`)
    .join("\n\n");
  const sources = report.report_json?.source_urls?.length
    ? `\n\n## Sources\n${report.report_json.source_urls.map((source) => `- ${source}`).join("\n")}`
    : "";
  return `# ${title}\n\n${body}${sources}`;
}

function mapReportType(report: ApiReportDetail): ReportType {
  const value = `${report.run_id ?? ""} ${report.metadata?.workflow_id ?? ""}`.toLowerCase();
  if (value.includes("weekly")) return "weekly";
  if (value.includes("quality")) return "quality";
  if (value.includes("source")) return "source_health";
  return "daily";
}

function mapReportStatus(status?: string | null): ReportStatus {
  const value = (status ?? "").toLowerCase();
  if (["final", "published", "publish", "pass", "passed", "succeeded", "success"].includes(value)) return "published";
  if (["review", "reviewed", "human_review", "approval_required"].includes(value)) return "reviewed";
  if (["blocked", "failed", "error"].includes(value)) return "failed";
  if (value === "draft") return "draft";
  return "generated";
}
