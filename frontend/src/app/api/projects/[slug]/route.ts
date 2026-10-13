import { NextRequest, NextResponse } from "next/server"
import { getProjectDetail } from "@/lib/projects/data-source"

export const dynamic = "force-dynamic"

export async function GET(_request: NextRequest, props: { params: Promise<{ slug: string }> }) {
  const params = await props.params;
  try {
    const result = await getProjectDetail(decodeURIComponent(params.slug))
    if (!result) {
      return NextResponse.json(
        {
          success: false,
          data: null,
          error: {
            code: "project_not_found",
            message: "Project not found",
          },
        },
        { status: 404 }
      )
    }
    return NextResponse.json({ success: true, data: result, error: null })
  } catch (error) {
    return NextResponse.json(
      {
        success: false,
        data: null,
        error: {
          code: "project_request_failed",
          message: error instanceof Error ? error.message : "Project request failed",
        },
      },
      { status: 500 }
    )
  }
}
