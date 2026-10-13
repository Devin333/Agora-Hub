import { NextRequest } from "next/server"
import { proxyBackendBinary } from "@/lib/paper-reader/server-proxy"
import { requirePublicPaper } from "@/lib/papers/public-route-guard"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest, props: { params: Promise<{ paperId: string }> }) {
  const params = await props.params;
  const guard = await requirePublicPaper(params.paperId)
  if (!guard.ok) {
    return guard.response
  }

  const page = request.nextUrl.searchParams.get("page")
  const bbox = request.nextUrl.searchParams.get("bbox")
  const query = new URLSearchParams()
  if (page) query.set("page", page)
  if (bbox) query.set("bbox", bbox)
  return proxyBackendBinary(`/api/v1/papers/${encodeURIComponent(guard.paper.id)}/source-preview?${query.toString()}`)
}
