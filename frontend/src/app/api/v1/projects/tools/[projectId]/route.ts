import { NextRequest } from "next/server"
import { getProjectList } from "@/lib/projects/data-source"
import { backendGet, backendPath, failure, findTool, projectParams, success } from "@/lib/projects/v1-route-data"
import type { ProjectsApiTool } from "@/types/projects"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest, props: { params: Promise<{ projectId: string }> }) {
  const params = await props.params;
  const path = `/api/v1/projects/tools/${encodeURIComponent(params.projectId)}`
  const backend = await backendGet<ProjectsApiTool>(backendPath(path, request.nextUrl.searchParams))
  if (backend.ok) return success(backend.data)

  try {
    const result = await getProjectList(projectParams(request.nextUrl.searchParams, { sort: "activity", limit: 100 }))
    const tool = findTool(result, params.projectId)
    if (!tool) return failure(404, "project_tool_not_found", `Project tool not found: ${params.projectId}`)
    return success(tool)
  } catch (error) {
    return failure(500, "project_tool_fallback_failed", error instanceof Error ? error.message : "Project tool fallback failed")
  }
}
