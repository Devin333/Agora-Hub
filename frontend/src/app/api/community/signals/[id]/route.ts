import { NextRequest, NextResponse } from "next/server"
import { getCommunitySignal } from "@/lib/community/server-data"

export const dynamic = "force-dynamic"

export async function GET(_request: NextRequest, props: { params: Promise<{ id: string }> }) {
  const params = await props.params;
  try {
    const detail = await getCommunitySignal(decodeURIComponent(params.id))
    if (!detail) {
      return NextResponse.json(
        {
          success: false,
          error: {
            code: "community_signal_not_found",
            message: "Community Pulse signal was not found."
          }
        },
        { status: 404 }
      )
    }

    return NextResponse.json({ success: true, data: detail })
  } catch (error) {
    return NextResponse.json(
      {
        success: false,
        error: {
          code: "community_signal_request_failed",
          message: error instanceof Error ? error.message : "Community Pulse signal request failed"
        }
      },
      { status: 500 }
    )
  }
}
