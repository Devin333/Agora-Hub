import { NextRequest, NextResponse } from "next/server"
import { loadPaperCompileStatus } from "@/lib/paper-reader/server-loader"

export const dynamic = "force-dynamic"

export async function GET(_request: NextRequest, props: { params: Promise<{ paperId: string }> }) {
  const params = await props.params;
  const status = await loadPaperCompileStatus(params.paperId)
  if (status) {
    return NextResponse.json({ success: true, data: { status } })
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
