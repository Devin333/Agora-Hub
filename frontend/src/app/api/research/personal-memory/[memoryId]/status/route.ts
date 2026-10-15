import { NextRequest } from "next/server"
import { desktopFailure, desktopPost } from "../../_server"
import { isPersonalMemoryId, sanitizePersonalMemoryStatus } from "@/lib/research/personal-memory-contracts"

export const dynamic = "force-dynamic"
export async function POST(request: NextRequest, props: { params: Promise<{ memoryId: string }> }) {
  const { memoryId } = await props.params
  if (!isPersonalMemoryId(memoryId)) return desktopFailure("invalid_request", 400)
  return desktopPost(`/api/v1/research/desktop/memory/${encodeURIComponent(memoryId)}/status`, request, sanitizePersonalMemoryStatus)
}
