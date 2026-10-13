import { NextRequest } from "next/server"
import { desktopFailure, desktopGet, safeDesktopId } from "@/lib/research/desktop/server-proxy"
export const dynamic = "force-dynamic"
export async function GET(request: NextRequest, props: { params: Promise<{ runId: string }> }) {
  const params = await props.params;
  if (!safeDesktopId(params.runId)) return desktopFailure("invalid_request", 400)
  const revision = request.nextUrl.searchParams.get("sourceRevision")
  if (!revision || revision.length > 512) return desktopFailure("invalid_request", 400)
  return desktopGet(`/api/v1/research/desktop/reports/runs/${encodeURIComponent(params.runId)}?sourceRevision=${encodeURIComponent(revision)}`, request)
}
