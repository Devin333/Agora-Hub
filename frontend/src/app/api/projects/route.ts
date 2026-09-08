import { NextRequest, NextResponse } from "next/server"
import { getProjectList } from "@/lib/projects/data-source"
import type { ProjectListParams } from "@/types/projects"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest) {
  try {
    const result = await getProjectList(projectParams(request.nextUrl.searchParams))
    return NextResponse.json({ success: true, data: result, error: null })
  } catch (error) {
    return NextResponse.json(
      {
        success: false,
        data: null,
        error: {
          code: "projects_request_failed",
          message: error instanceof Error ? error.message : "Projects request failed",
        },
      },
      { status: 500 }
    )
  }
}

function projectParams(searchParams: URLSearchParams): ProjectListParams {
  return {
    q: searchParams.get("q") ?? undefined,
    category: searchParams.get("category") as ProjectListParams["category"],
    topic: searchParams.get("topic") ?? undefined,
    sort: searchParams.get("sort") as ProjectListParams["sort"],
    source: searchParams.get("source") as ProjectListParams["source"],
    language: searchParams.get("language") as ProjectListParams["language"],
    license: searchParams.get("license") as ProjectListParams["license"],
    maturity: searchParams.get("maturity") as ProjectListParams["maturity"],
    period: searchParams.get("period") as ProjectListParams["period"],
    page: numberParam(searchParams.get("page")),
    pageSize: numberParam(searchParams.get("pageSize")),
    limit: numberParam(searchParams.get("limit")),
    cursor: searchParams.get("cursor") ?? undefined,
  }
}

function numberParam(value: string | null): number | undefined {
  if (!value) return undefined
  const number = Number(value)
  return Number.isFinite(number) ? number : undefined
}
