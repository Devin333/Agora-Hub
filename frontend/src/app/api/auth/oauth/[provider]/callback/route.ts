import { randomBytes, timingSafeEqual } from "node:crypto"
import { NextRequest, NextResponse } from "next/server"
import { safeApiPost } from "@/lib/api/server"
import { AUTH_BINDING_COOKIE, AUTH_CALLBACK_COOKIE, AUTH_RETURN_COOKIE, authFailure, isSameOrigin, requestOrigin, safeReturnPath, validProvider } from "@/lib/auth/portal-server"
import { NEWSROOM_SESSION_COOKIE, sessionCookieOptions } from "@/lib/auth/session"

export const dynamic = "force-dynamic"

export async function GET(request: NextRequest, { params }: { params: { provider: string } }) {
  const state = request.nextUrl.searchParams.get("state") ?? ""
  const code = request.nextUrl.searchParams.get("code") ?? ""
  const rejected = request.nextUrl.searchParams.has("error")
  // Cross-site iframe navigations omit SameSite=Lax cookies. An intermediate
  // same-origin document posts the response before exchanging any provider code.
  if (params.provider === "wechat" && request.headers.get("sec-fetch-dest") === "iframe") {
    const nonce = randomBytes(16).toString("base64url")
    const escape = (value: string) => value.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    const invalid = rejected || state.length > 512 || code.length > 2048
    const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>正在完成微信登录</title><form method="post" action="/api/auth/oauth/wechat/callback"><input type="hidden" name="state" value="${escape(state.slice(0, 512))}"><input type="hidden" name="code" value="${escape(invalid ? "" : code)}"><button type="submit">继续完成微信登录</button></form><script nonce="${nonce}">document.forms[0].submit()</script></html>`
    return new NextResponse(html, { headers: {
      "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Referrer-Policy": "origin",
      "Content-Security-Policy": `default-src 'none'; script-src 'nonce-${nonce}'; form-action 'self'; frame-ancestors 'self'; base-uri 'none'`,
    } })
  }
  return completeCallback(request, params.provider, code, state, rejected)
}

export async function POST(request: NextRequest, { params }: { params: { provider: string } }) {
  if (params.provider !== "wechat" || !isSameOrigin(request)) return authFailure("auth_invalid_origin", 403)
  const form = await request.formData().catch(() => null)
  const code = form?.get("code")
  const state = form?.get("state")
  if (typeof code !== "string" || typeof state !== "string") return authFailure("auth_invalid_request")
  return completeCallback(request, params.provider, code, state, false)
}

async function completeCallback(request: NextRequest, provider: string, code: string, state: string, rejected: boolean) {
  const expected = request.cookies.get(AUTH_CALLBACK_COOKIE)?.value ?? ""
  const binding = request.cookies.get(AUTH_BINDING_COOKIE)?.value ?? ""
  const validState = Boolean(state && expected && state.length < 512 && Buffer.byteLength(state) === Buffer.byteLength(expected) && timingSafeEqual(Buffer.from(state), Buffer.from(expected)))
  let token: string | undefined
  let error = "auth_provider_rejected"
  if (validProvider(provider) && validState && /^[A-Za-z0-9_-]{43}$/.test(binding) && code && code.length <= 2048 && !rejected) {
    const result = await safeApiPost<{ session: { sessionToken?: string } }>(`/api/v1/auth/oauth/${provider}/callback`, { code, state }, {
      headers: { "X-Newsroom-Auth-Binding": binding }, signal: AbortSignal.timeout(20000),
    })
    if (result.ok) token = result.data.session?.sessionToken
    else if (result.errorCode === "auth_provider_unavailable") error = result.errorCode
  }
  const nonce = randomBytes(16).toString("base64url")
  const returnTo = safeReturnPath(request.cookies.get(AUTH_RETURN_COOKIE)?.value)
  const payload = JSON.stringify({ type: "agora-auth-complete", provider, state: state.slice(0, 512), success: Boolean(token), error: token ? null : error }).replace(/</g, "\\u003c")
  const origin = JSON.stringify(requestOrigin(request) ?? request.nextUrl.origin).replace(/</g, "\\u003c")
  const href = returnTo.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  const html = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Agora AI</title><style nonce="${nonce}">body{margin:0;min-height:100vh;display:grid;place-items:center;background:#fbf8ff;color:#302640;font:16px Arial,sans-serif;text-align:center}h1{font-size:24px}a{color:#7c3aed;line-height:3}</style><main><h1>${token ? "登录成功" : "登录未完成"}</h1><p>${token ? "可以返回原页面继续研究。" : "请返回原页面，重试或选择其他登录方式。"}</p><a href="${href}" target="_top" rel="noreferrer">返回研究</a></main><script nonce="${nonce}">const message=${payload};const targetOrigin=${origin};if(window.opener){window.opener.postMessage(message,targetOrigin);window.close()}else if(window.parent!==window){window.parent.postMessage(message,targetOrigin)}else if(message.success){window.location.replace(${JSON.stringify(returnTo).replace(/</g, "\\u003c")})}</script></html>`
  const response = new NextResponse(html, { headers: {
    "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": `default-src 'none'; style-src 'nonce-${nonce}'; script-src 'nonce-${nonce}'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'`,
  } })
  if (token) response.cookies.set(NEWSROOM_SESSION_COOKIE, token, sessionCookieOptions())
  if (validState) {
    response.cookies.delete(AUTH_CALLBACK_COOKIE)
    response.cookies.delete(AUTH_RETURN_COOKIE)
  }
  return response
}
