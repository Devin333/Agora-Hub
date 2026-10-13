import { NextRequest, NextResponse } from "next/server"
import { loadReaderWorkspace } from "@/features/portal/reader/reader-server"

export const dynamic = "force-dynamic"

export async function GET(_request: NextRequest, props: { params: Promise<{ paperId: string }> }) {
  const params = await props.params;
  const payload = await loadReaderWorkspace(params.paperId)
  return payload ? NextResponse.json({ success: true, data: payload }) : NextResponse.json({ success: false, error: { code: "paper_not_found", message: "Paper not found" } }, { status: 404 })
}
