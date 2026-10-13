import { NextRequest } from "next/server"
import { getProjectList } from "@/lib/projects/data-source"
import {
  backendGet,
  backendPath,
  buildProjectDetail,
  failure,
  findProject,
  projectParams,
  success,
} from "@/lib/projects/v1-route-data"
import type { ProjectsApiProjectDetail } from "@/types/projects"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest, props: { params: Promise<{ projectId: string }> }) {
  const params = await props.params;
  const path = `/api/v1/projects/${encodeURIComponent(params.projectId)}`
  const backend = await backendGet<ProjectsApiProjectDetail>(backendPath(path, request.nextUrl.searchParams))
  if (backend.ok) return success(backend.data)

  try {
    const result = await getProjectList(projectParams(request.nextUrl.searchParams, { limit: 100 }))
    const project = findProject(result, params.projectId)
    if (!project) return failure(404, "project_not_found", `Project not found: ${params.projectId}`)
    return success(buildProjectDetail(project, result))
  } catch (error) {
    return failure(500, "project_detail_fallback_failed", error instanceof Error ? error.message : "Project detail fallback failed")
  }
}
