import { NextRequest } from "next/server"
import { desktopFailure, desktopGet, safeDesktopId } from "@/lib/research/desktop/server-proxy"
export const dynamic = "force-dynamic"
export async function GET(request: NextRequest, props: { params: Promise<{ requestId: string }> }) {
  const params = await props.params;
  if (!safeDesktopId(params.requestId)) return desktopFailure("invalid_request", 400)
  const generation = request.nextUrl.searchParams.get("generation")
  if (!generation || !/^gen_[0-9a-f]{32}$/.test(generation)) return desktopFailure("desktop_generation_invalid", 400)
  return desktopGet(`/api/v1/research/desktop/operations/${encodeURIComponent(params.requestId)}?generation=${encodeURIComponent(generation)}`, request)
}
