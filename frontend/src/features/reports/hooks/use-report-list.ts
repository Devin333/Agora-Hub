"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { MockHookResult } from "@/types/common";
import type { Report, ReportStatus, ReportType } from "@/types/report";

export type ReportFilters = {
  keyword?: string;
  reportType?: ReportType;
  status?: ReportStatus;
};

export function useReportList(filters: ReportFilters = {}): MockHookResult<Report[]> {
  const { data: apiReports, error, isLoading, isError, refetch } = useQuery({
    queryKey: ["reports", "search", filters.keyword?.trim() ?? ""],
    queryFn: ({ signal }) => fetchReports(filters.keyword, signal),
    retry: false,
  });

  const data = useMemo(() => {
    return (apiReports ?? [])
      .filter((report) => (!filters.reportType ? true : (report.reportType ?? report.type) === filters.reportType))
      .filter((report) => (!filters.status ? true : report.status === filters.status))
      .sort((a, b) => new Date(b.generatedAt).getTime() - new Date(a.generatedAt).getTime());
  }, [apiReports, filters.reportType, filters.status]);

  return {
    data,
    isLoading,
    isError,
    error: error ?? undefined,
    refetch: () => {
      void refetch();
    },
  };
}

type ApiEnvelope<T> = {
  success?: boolean;
  data?: T | null;
  error?: { message?: string | null } | null;
};

type ApiReportList = {
  reports?: ApiReportSummary[];
};

type ApiReportSummary = {
  report_id?: string | null;
  id?: string | null;
  run_id?: string | null;
  title?: string | null;
  status?: string | null;
  finished_at?: string | null;
  published_at?: string | null;
  created_at?: string | null;
  quality_score?: number | null;
  workflow_id?: string | null;
  profile?: string | null;
};

async function fetchReports(keyword?: string, signal?: AbortSignal): Promise<Report[]> {
  const query = keyword?.trim();
  const path = query
    ? `/api/v1/search/reports?q=${encodeURIComponent(query)}&limit=50`
    : "/api/v1/reports?limit=50";
  const response = await fetch(path, {
    signal,
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  const payload = await response.json() as ApiEnvelope<ApiReportList> | ApiReportList;
  if (!response.ok) {
    throw new Error(`Reports API failed: ${response.status}`);
  }
  const data = unwrapEnvelope(payload);
  return (data.reports ?? []).map(mapReportSummary);
}

function unwrapEnvelope<T>(payload: ApiEnvelope<T> | T): T {
  if (typeof payload === "object" && payload !== null && "success" in payload) {
    if (payload.success === false) {
      throw new Error(payload.error?.message ?? "Reports API returned an error");
    }
    return (payload.data ?? {}) as T;
  }
  return payload as T;
}

function mapReportSummary(report: ApiReportSummary): Report {
  const generatedAt = report.finished_at ?? report.published_at ?? report.created_at ?? new Date(0).toISOString();
  const reportType = mapReportType(report);
  return {
    id: report.report_id ?? report.id ?? report.run_id ?? "unknown-report",
    title: report.title ?? report.report_id ?? "Untitled report",
    type: reportType,
    reportType,
    generatedAt,
    coveredFrom: generatedAt,
    coveredTo: generatedAt,
    agentName: report.workflow_id ?? "Agora Hub Runtime",
    qualityScore: typeof report.quality_score === "number" ? report.quality_score : undefined,
    topicIds: [],
    newsItemIds: [],
    evidenceIds: [],
    status: mapReportStatus(report.status),
  };
}

function mapReportType(report: ApiReportSummary): ReportType {
  const value = `${report.profile ?? ""} ${report.workflow_id ?? ""}`.toLowerCase();
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
