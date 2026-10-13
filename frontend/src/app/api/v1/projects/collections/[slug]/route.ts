import { NextRequest } from "next/server"
import { getProjectList } from "@/lib/projects/data-source"
import { backendGet, backendPath, failure, findCollection, projectParams, success } from "@/lib/projects/v1-route-data"
import type { ProjectsApiCollection } from "@/types/projects"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest, props: { params: Promise<{ slug: string }> }) {
  const params = await props.params;
  const path = `/api/v1/projects/collections/${encodeURIComponent(params.slug)}`
  const backend = await backendGet<ProjectsApiCollection>(backendPath(path, request.nextUrl.searchParams))
  if (backend.ok) return success(backend.data)

  try {
    const result = await getProjectList(projectParams(request.nextUrl.searchParams, { limit: 100 }))
    const collection = findCollection(result, params.slug)
    if (!collection) return failure(404, "project_collection_not_found", `Project collection not found: ${params.slug}`)
    return success(collection)
  } catch (error) {
    return failure(500, "project_collection_fallback_failed", error instanceof Error ? error.message : "Project collection fallback failed")
  }
}
