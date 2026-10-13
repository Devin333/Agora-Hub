import { NextRequest, NextResponse } from "next/server"
import { safeApiPost } from "@/lib/api/server"
import { requirePaperOpsSession } from "@/lib/papers/ops-route-guard"
import { requirePublicPaper } from "@/lib/papers/public-route-guard"

export const dynamic = "force-dynamic"

export async function POST(request: NextRequest, props: { params: Promise<{ paperId: string }> }) {
  const params = await props.params;
  const guard = await requirePublicPaper(params.paperId)
  if (!guard.ok) {
    return guard.response
  }

  const opsGuard = await requirePaperOpsSession()
  if (opsGuard) {
    return opsGuard
  }

  const body = await request.json().catch(() => ({}))
  const result = await safeApiPost(`/api/v1/papers/${encodeURIComponent(guard.paper.id)}/compile`, body)
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
      },
    },
    { status: result.errorCode === "paper_not_found" ? 404 : 502 },
  )
}
