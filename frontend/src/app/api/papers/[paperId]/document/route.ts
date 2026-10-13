import { NextRequest, NextResponse } from "next/server"
import { loadPaperDocumentPayload } from "@/lib/paper-reader/server-loader"

export const dynamic = "force-dynamic"

export async function GET(_request: NextRequest, props: { params: Promise<{ paperId: string }> }) {
  const params = await props.params;
  const payload = await loadPaperDocumentPayload(params.paperId)
  if (payload) {
    return NextResponse.json({ success: true, data: payload })
  }
  return NextResponse.json(
    {
      success: false,
      error: {
        code: "paper_not_found",
        message: "Paper not found",
      },
    },
    { status: 404 },
  )
}
