import { NextRequest } from "next/server"
import { getProjectList } from "@/lib/projects/data-source"
import { backendGet, backendPath, failure, findCase, projectParams, success } from "@/lib/projects/v1-route-data"
import type { ProjectsApiCase } from "@/types/projects"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest, props: { params: Promise<{ caseId: string }> }) {
  const params = await props.params;
  const path = `/api/v1/projects/cases/${encodeURIComponent(params.caseId)}`
  const backend = await backendGet<ProjectsApiCase>(backendPath(path, request.nextUrl.searchParams))
  if (backend.ok) return success(backend.data)

  try {
    const result = await getProjectList(projectParams(request.nextUrl.searchParams, { sort: "quality", limit: 100 }))
    const item = findCase(result, params.caseId)
    if (!item) return failure(404, "project_case_not_found", `Project case not found: ${params.caseId}`)
    return success(item)
  } catch (error) {
    return failure(500, "project_case_fallback_failed", error instanceof Error ? error.message : "Project case fallback failed")
  }
}
