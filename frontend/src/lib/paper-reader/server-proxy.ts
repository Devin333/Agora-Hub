import { NextResponse } from "next/server"
import { apiBaseUrl } from "@/lib/deployment-profile"

export async function proxyBackendBinary(path: string) {
  const response = await fetch(backendUrl(path), {
    method: "GET",
    headers: requestHeaders(),
    cache: "no-store",
  })
  if (!response.ok) {
    const contentType = response.headers.get("content-type") ?? ""
    const detail = contentType.includes("application/json") ? await response.json() : await response.text()
    return NextResponse.json(
      {
        success: false,
        error: {
          code: `backend_${response.status}`,
          message: response.statusText || "Backend request failed",
          detail,
        },
      },
      { status: response.status },
    )
  }
  return new NextResponse(response.body, {
    status: response.status,
    headers: {
      "Content-Type": response.headers.get("content-type") ?? "application/octet-stream",
      "Cache-Control": "public, max-age=31536000, immutable",
    },
  })
}

function backendUrl(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) return path
  const suffix = path.startsWith("/") ? path : `/${path}`
  return `${apiBaseUrl()}${suffix}`
}

function requestHeaders(): HeadersInit {
  const headers: Record<string, string> = {
    Accept: "*/*",
  }
  const apiToken = process.env.NEWSROOM_API_TOKEN ?? process.env.NEWS_API_TOKEN
  if (apiToken) {
    headers.Authorization = `Bearer ${apiToken}`
  }
  return headers
}
