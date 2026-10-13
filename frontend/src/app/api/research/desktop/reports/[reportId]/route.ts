import { NextRequest } from "next/server"
import { desktopFailure, desktopGet, safeDesktopId } from "@/lib/research/desktop/server-proxy"
export const dynamic = "force-dynamic"
export async function GET(request: NextRequest, props: { params: Promise<{ reportId: string }> }) {
  const params = await props.params;
  if (!safeDesktopId(params.reportId)) return desktopFailure("invalid_request", 400)
  return desktopGet(`/api/v1/research/desktop/reports/${encodeURIComponent(params.reportId)}`, request)
}
