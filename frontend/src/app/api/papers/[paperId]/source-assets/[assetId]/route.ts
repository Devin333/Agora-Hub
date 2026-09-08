import { NextResponse } from "next/server"
import { getPaperById } from "@/lib/papers/real-data"
import { loadReaderSource } from "@/features/portal/reader/reader-source-server"

export const dynamic = "force-dynamic"

export async function GET(request: Request, { params }: { params: { paperId: string; assetId: string } }) {
  const paper = await getPaperById(params.paperId)
  if (!paper || paper.isPublished === false) return NextResponse.json({ success: false }, { status: 404 })
  const source = await loadReaderSource(paper)
  const asset = source?.assets.get(params.assetId)
  if (!asset) return NextResponse.json({ success: false }, { status: 404 })
  const version = new URL(request.url).searchParams.get("v")
  if (version && version !== asset.checksum) return NextResponse.json({ success: false }, { status: 404 })
  return new NextResponse(Buffer.from(asset.bytes), { headers: {
    "Content-Type": asset.mime,
    "Content-Length": String(asset.bytes.length),
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
    "Cache-Control": "public, max-age=1800",
    "ETag": `"${asset.checksum}"`,
  } })
}
