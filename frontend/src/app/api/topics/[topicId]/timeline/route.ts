import { NextRequest, NextResponse } from "next/server"
import { evidenceGraphQueryFromSearchParams, getEvidenceGraphTimeline } from "@/features/evidence-graph/evidence-graph-data"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest, props: { params: Promise<{ topicId: string }> }) {
  const params = await props.params;
  try {
    const query = evidenceGraphQueryFromSearchParams(request.nextUrl.searchParams)
    const data = await getEvidenceGraphTimeline(decodeURIComponent(params.topicId), query)
    return NextResponse.json({ success: true, data, error: null })
  } catch (error) {
    return NextResponse.json(
      {
        success: false,
        data: null,
        error: {
          code: "topic_timeline_request_failed",
          message: error instanceof Error ? error.message : "Topic timeline request failed",
        },
      },
      { status: 500 }
    )
  }
}
