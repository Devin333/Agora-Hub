import { cookies } from "next/headers"
import { NextRequest, NextResponse } from "next/server"
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session"
import { isSameOrigin } from "@/lib/auth/portal-server"
import { safeApiPost } from "@/lib/api/server"
import { researchIntentSchema, researchSearchResponseSchema } from "./conversation"
import { cachedReaderReady } from "@/features/portal/reader/reader-source-server"

const privateHeaders = { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" }
export async function guidedResearchProxy(request: NextRequest, operation: "intent" | "search") {
  const failure = (message: string, status: number) => NextResponse.json({ success: false, error: { code: "guided_research_failed", message } }, { status, headers: privateHeaders })
  if (!isSameOrigin(request)) return failure("请从当前页面发起研究。", 403)
  const maxBytes = 256 * 1024
  if (Number(request.headers.get("content-length")) > maxBytes) return failure("本次内容过多，请新建研究或减少资料。", 413)
  const reader = request.body?.getReader()
  if (!reader) return failure("请先输入问题。", 400)
  let size = 0
  const chunks: Uint8Array[] = []
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      size += chunk.value.byteLength
      if (size > maxBytes) { await reader.cancel(); return failure("本次内容过多，请新建研究或减少资料。", 413) }
      chunks.push(chunk.value)
    }
  } catch { return failure("暂时无法读取本次问题，请重试。", 400) }
  const bytes = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength }
  let body: unknown
  try { body = JSON.parse(new TextDecoder().decode(bytes)) } catch { return failure("问题格式无效，请重试。", 400) }
  if (!body || typeof body !== "object" || Array.isArray(body)) return failure("问题格式无效，请重试。", 400)
  const token = cookies().get(NEWSROOM_SESSION_COOKIE)?.value
  const response = await safeApiPost(`/api/v1/research/guided/${operation}`, body, { headers: token ? { "x-newsroom-session": token } : {}, signal: AbortSignal.any([request.signal, AbortSignal.timeout(60000)]) })
  if (!response.ok) return failure(response.status && response.status < 500 ? response.errorMessage : "研究服务暂时不可用，请稍后重试。", response.status ?? 503)
  const parsed = (operation === "intent" ? researchIntentSchema : researchSearchResponseSchema).safeParse(response.data)
  if (!parsed.success) return failure("返回的研究资料格式不完整，请重试。", 502)
  if ("results" in parsed.data && parsed.data.source === "papers") {
    parsed.data.results = parsed.data.results.map(result => ({ ...result, href: /^[A-Za-z0-9_-]+$/.test(result.id) && cachedReaderReady(result.id) ? `/design-demo/papers/${result.id}/read` : undefined }))
  }
  return NextResponse.json({ success: true, data: parsed.data }, { headers: privateHeaders })
}
