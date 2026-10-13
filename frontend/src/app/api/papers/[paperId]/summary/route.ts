import { NextRequest, NextResponse } from "next/server"
import { safeApiPost } from "@/lib/api/server"
import { requirePublicPaper } from "@/lib/papers/public-route-guard"

export const dynamic = "force-dynamic"

export async function POST(request: NextRequest, props: { params: Promise<{ paperId: string }> }) {
  const params = await props.params;
  const guard = await requirePublicPaper(params.paperId)
  if (!guard.ok) {
    return guard.response
  }

  const locale = request.nextUrl.searchParams.get("locale") ?? "en"
  const refresh = request.nextUrl.searchParams.get("refresh") === "true"
  const searchParams = new URLSearchParams({ locale })
  if (refresh) {
    searchParams.set("refresh", "true")
  }
  const result = await safeApiPost(
    `/api/v1/papers/${encodeURIComponent(guard.paper.id)}/summary?${searchParams.toString()}`
  )
  if (result.ok) {
    return NextResponse.json({ success: true, data: result.data })
  }
  return NextResponse.json(
    {
      success: false,
      error: {
        code: result.errorCode,
        message: result.errorMessage,
        requestId: result.requestId,
        retryable: result.errorCode === "paper_summary_unavailable",
      },
    },
    { status: result.errorCode === "paper_summary_unavailable" ? 503 : 502 }
  )
}
