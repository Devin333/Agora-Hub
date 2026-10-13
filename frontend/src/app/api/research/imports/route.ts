import { cookies } from "next/headers"
import { NextRequest, NextResponse } from "next/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { authFailure, isSameOrigin } from "@/lib/auth/portal-server"
import { desktopBinaryPost, desktopFailure } from "@/lib/research/desktop/server-proxy"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"

export const dynamic = "force-dynamic"
const MAX_BODY_BYTES = 50 * 1024 * 1024
const backend = process.env.NEWSROOM_API_BASE_URL ?? "http://127.0.0.1:8000"
export async function POST(request: NextRequest) {
  if (!isSameOrigin(request)) return authFailure("auth_invalid_origin", 403)
  const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value
  if (!token) return authFailure("auth_required", 401)
  const bytes = await readPdf(request)
  if (!bytes) return desktopFailure("invalid_pdf", 400, "PDF 内容无效或超过 50 MB。")
  if (isLocalDesktopProfile()) {
    const headers: Record<string, string> = {}
    for (const [browser, backendHeader] of [["x-newsroom-request-id", "X-Newsroom-Request-Id"], ["x-newsroom-generation", "X-Newsroom-Generation"], ["x-newsroom-request-fingerprint", "X-Newsroom-Request-Fingerprint"], ["x-newsroom-source-revision", "X-Newsroom-Source-Revision"], ["x-newsroom-source-fingerprint", "X-Newsroom-Source-Fingerprint"], ["x-filename", "X-Filename"]] as const) {
      const value = request.headers.get(browser); if (!value) return desktopFailure("invalid_request", 400, `缺少 ${backendHeader}。`); headers[backendHeader] = value
    }
    if (!/^sha256:[0-9a-f]{64}$/.test(headers["X-Newsroom-Source-Fingerprint"]) || !/^sha256:[0-9a-f]{64}$/.test(headers["X-Newsroom-Request-Fingerprint"]) || !/^gen_[0-9a-f]{32}$/.test(headers["X-Newsroom-Generation"]) || !/^[A-Za-z0-9:_-]{8,128}$/.test(headers["X-Newsroom-Request-Id"]) || headers["X-Newsroom-Source-Revision"].length > 512 || headers["X-Filename"].length > 500) return desktopFailure("invalid_request", 400)
    return desktopBinaryPost("/api/v1/research/desktop/operations/import", request, bytes, { ...headers, "Content-Type": "application/pdf" })
  }
  const response = await fetch(`${backend}/api/v1/research/imports`, { method: "POST", headers: { "Content-Type": "application/pdf", "X-Filename": request.headers.get("x-filename") || "paper.pdf", "X-Newsroom-Session": token }, body: bytes, cache: "no-store", signal: AbortSignal.any([request.signal, AbortSignal.timeout(120000)]) }).catch(() => null)
  if (!response) return desktopFailure("research_service_unavailable", 503, "上传暂时不可用，请稍后重试。")
  return new NextResponse(response.body, { status: response.status, headers: { "Content-Type": response.headers.get("content-type") || "application/json", "Cache-Control": "no-store" } })
}
async function readPdf(request: NextRequest): Promise<ArrayBuffer | null> {
  const length = Number(request.headers.get("content-length") ?? "0"); if (Number.isFinite(length) && length > MAX_BODY_BYTES) return null
  const reader = request.body?.getReader(); if (!reader) return null
  const chunks: Uint8Array[] = []; let size = 0
  try { while (true) { const chunk = await reader.read(); if (chunk.done) break; size += chunk.value.byteLength; if (size > MAX_BODY_BYTES) { await reader.cancel(); return null }; chunks.push(chunk.value) } } catch { return null }
  const body = new Uint8Array(size); let offset = 0; for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength }
  if (body.length < 5 || new TextDecoder().decode(body.slice(0, 5)) !== "%PDF-") return null
  return body.buffer
}
