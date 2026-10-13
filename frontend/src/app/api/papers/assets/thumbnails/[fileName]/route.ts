import { NextRequest, NextResponse } from "next/server"

export const dynamic = "force-dynamic"
export const runtime = "nodejs"

const DEFAULT_API_BASE_URL = "http://localhost:8000"

export async function GET(_request: NextRequest, props: { params: Promise<{ fileName: string }> }) {
  const params = await props.params;
  const { fileName } = params
  const response = await fetch(backendUrl(`/api/v1/papers/assets/thumbnails/${encodeURIComponent(fileName)}`), {
    headers: requestHeaders(),
    cache: "no-store",
  })
  if (!response.ok) {
    return NextResponse.json(
      {
        success: false,
        error: {
          code: `http_${response.status}`,
          message: response.statusText || "Paper thumbnail unavailable",
        },
        request_id: null,
      },
      { status: response.status }
    )
  }
  const body = await response.arrayBuffer()
  return new NextResponse(body, {
    status: 200,
    headers: {
      "Content-Type": response.headers.get("content-type") ?? "image/png",
      "Cache-Control": "public, max-age=3600",
    },
  })
}

function backendUrl(path: string): string {
  const baseUrl = process.env.NEWSROOM_API_BASE_URL ?? DEFAULT_API_BASE_URL
  const suffix = path.startsWith("/") ? path : `/${path}`
  return `${baseUrl.replace(/\/$/, "")}${suffix}`;
}

function requestHeaders(): HeadersInit {
  const apiToken = process.env.NEWSROOM_API_TOKEN ?? process.env.NEWS_API_TOKEN
  return apiToken ? { Authorization: `Bearer ${apiToken}` } : {}
}
