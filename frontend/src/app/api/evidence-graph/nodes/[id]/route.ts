import { NextRequest, NextResponse } from "next/server"
import { evidenceGraphQueryFromSearchParams, getEvidenceGraphNodeDetail } from "@/features/evidence-graph/evidence-graph-data"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest, props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  try {
    const query = evidenceGraphQueryFromSearchParams(request.nextUrl.searchParams)
    const data = await getEvidenceGraphNodeDetail(decodeURIComponent(params.id), query)
    if (!data) {
      return NextResponse.json(
        {
          success: false,
          data: null,
          error: {
            code: "evidence_graph_node_not_found",
            message: "Evidence graph node was not found.",
          },
        },
        { status: 404 }
      )
    }
    return NextResponse.json({ success: true, data, error: null })
  } catch (error) {
    return NextResponse.json(
      {
        success: false,
        data: null,
        error: {
          code: "evidence_graph_node_request_failed",
          message: error instanceof Error ? error.message : "Evidence graph node request failed",
        },
      },
      { status: 500 }
    )
  }
}
