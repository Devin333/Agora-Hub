import { NextRequest } from "next/server"
import { AUTH_CALLBACK_COOKIE, AUTH_RETURN_COOKIE, authFailure, bindingOptions, proxyAuthPost, safeReturnPath, validProvider } from "@/lib/auth/portal-server"

export const dynamic = "force-dynamic"
export async function POST(request: NextRequest, { params }: { params: { provider: string } }) {
  if (!validProvider(params.provider)) return authFailure("auth_method_unavailable", 404)
  let returnTo = "/design-demo"
  const response = await proxyAuthPost(request, `/api/v1/auth/oauth/${params.provider}/authorize`, (body) => {
    if (body.consent !== true) return null
    returnTo = safeReturnPath(body.returnTo)
    return { consent: true }
  })
  if (response.ok) {
    const payload = await response.clone().json()
    let url: URL
    try { url = new URL(payload.data.authorizationUrl) } catch { return authFailure("auth_service_unavailable", 502) }
    const allowedHost = { google: "accounts.google.com", wechat: "open.weixin.qq.com", qq: "graph.qq.com" }[params.provider]
    const state = url.searchParams.get("state")
    if (!state || state.length > 512 || url.protocol !== "https:" || url.hostname !== allowedHost || url.username || url.password) return authFailure("auth_service_unavailable", 502)
    response.cookies.set(AUTH_RETURN_COOKIE, returnTo, bindingOptions)
    response.cookies.set(AUTH_CALLBACK_COOKIE, state, bindingOptions)
  }
  return response
}
