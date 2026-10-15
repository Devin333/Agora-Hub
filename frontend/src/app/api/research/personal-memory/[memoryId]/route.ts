import { NextRequest } from "next/server"
import { desktopFailure, desktopGet, personalMemoryDelete, personalMemoryPatch } from "../_server"
import { isPersonalMemoryId, sanitizePersonalMemoryRevision, sanitizePersonalMemoryUpdate } from "@/lib/research/personal-memory-contracts"

export const dynamic = "force-dynamic"
type RouteProps = { params: Promise<{ memoryId: string }> }

export async function GET(request: NextRequest, props: RouteProps) {
  const { memoryId } = await props.params
  if (!isPersonalMemoryId(memoryId)) return desktopFailure("invalid_request", 400)
  return desktopGet(`/api/v1/research/desktop/memory/${encodeURIComponent(memoryId)}`, request)
}
export async function PATCH(request: NextRequest, props: RouteProps) {
  const { memoryId } = await props.params
  if (!isPersonalMemoryId(memoryId)) return desktopFailure("invalid_request", 400)
  return personalMemoryPatch(`/api/v1/research/desktop/memory/${encodeURIComponent(memoryId)}`, request, sanitizePersonalMemoryUpdate)
}
export async function DELETE(request: NextRequest, props: RouteProps) {
  const { memoryId } = await props.params
  if (!isPersonalMemoryId(memoryId)) return desktopFailure("invalid_request", 400)
  return personalMemoryDelete(`/api/v1/research/desktop/memory/${encodeURIComponent(memoryId)}`, request, sanitizePersonalMemoryRevision)
}
