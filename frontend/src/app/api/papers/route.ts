import { NextRequest, NextResponse } from "next/server"
import { getPaperListResult } from "@/lib/papers/real-data"
import type { PaperPeriod, PaperSort } from "@/lib/papers/types"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest) {
  const params = request.nextUrl.searchParams
  const result = await getPaperListResult({
    paperType: params.get("paperType") === "survey" ? "survey" : undefined,
    q: params.get("q") ?? undefined,
    period: parsePeriod(params.get("period")),
    sort: parseSort(params.get("sort")),
    topic: params.get("topic") ?? undefined,
    from: params.get("from") ?? undefined,
    to: params.get("to") ?? undefined,
    task: params.get("task") ?? undefined,
    method: params.get("method") ?? undefined,
    has: params.get("has") ?? undefined,
    limit: numberParam(params.get("limit")) ?? numberParam(params.get("pageSize")),
    offset: numberParam(params.get("offset"))
  })
  return NextResponse.json({ success: true, data: result })
}

function parsePeriod(value: string | null): PaperPeriod | undefined {
  return value === "daily" || value === "weekly" || value === "monthly" || value === "all" ? value : undefined
}

function parseSort(value: string | null): PaperSort | undefined {
  return value === "trending" || value === "relevance" || value === "newest" || value === "most_cited" ? value : undefined
}

function numberParam(value: string | null) {
  if (!value) {
    return undefined
  }
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : undefined
}
