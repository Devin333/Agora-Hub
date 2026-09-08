import type { AuthSession } from "@/lib/papers/types"

export type OtpChannel = "phone" | "email"
export type OAuthProvider = "google" | "wechat" | "qq"
export type LoginMethod = OtpChannel | OAuthProvider
export type AuthMethods = {
  methods: Record<LoginMethod, { available: boolean; reason?: string }>
  termsUrl: string | null
  privacyUrl: string | null
}
export type OtpChallenge = { challengeId: string; expiresAt: string; resendAt: string }
export type OAuthAuthorization = { authorizationUrl: string; expiresAt: string }

export class PortalAuthError extends Error {
  constructor(public code: string, public retryAfter = 0) { super(code) }
}

// Account requests must never pass through the portal's optional sample-data resolver.
async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: body === undefined ? { Accept: "application/json" } : { Accept: "application/json", "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })
  const envelope = await response.json().catch(() => null)
  if (!response.ok || envelope?.success !== true || !envelope.data) {
    throw new PortalAuthError(envelope?.error?.code ?? "auth_service_unavailable", Number(response.headers.get("retry-after")) || 0)
  }
  return envelope.data as T
}

export const fetchLoginMethods = (signal?: AbortSignal) => request<AuthMethods>("/api/auth/methods", undefined, signal)
export const fetchPortalSession = (signal?: AbortSignal) => request<{ session: AuthSession | null }>("/api/auth/session", undefined, signal)
export const requestOtp = (channel: OtpChannel, destination: string, signal?: AbortSignal) => request<OtpChallenge>("/api/auth/otp/challenges", { channel, destination, consent: true }, signal)
export const verifyOtp = (challengeId: string, code: string, signal?: AbortSignal) => request<{ session: AuthSession }>(`/api/auth/otp/challenges/${encodeURIComponent(challengeId)}/verify`, { code }, signal)
export const authorizeProvider = (provider: OAuthProvider, returnTo: string, signal?: AbortSignal) => request<OAuthAuthorization>(`/api/auth/oauth/${provider}/authorize`, { returnTo, consent: true }, signal)
export const logoutPortal = () => request<{ revoked: boolean }>("/api/auth/logout", {})

export function authErrorMessage(error: unknown): string {
  const code = error instanceof PortalAuthError ? error.code : "auth_service_unavailable"
  return ({
    auth_method_unavailable: "该登录方式暂未开放，请选择其他方式。",
    auth_service_unavailable: "登录服务暂时不可用，请稍后重试。",
    auth_rate_limited: "操作过于频繁，请稍后重试。",
    auth_challenge_invalid: "验证码不正确或已过期，请检查后重试或重新获取。",
    auth_provider_rejected: "授权未完成，请重试或选择其他登录方式。",
    auth_provider_unavailable: "授权服务暂时不可用，请稍后重试。",
    auth_delivery_failed: "验证码发送失败，请稍后重新获取。",
    auth_invalid_request: "请检查输入的手机号或邮箱。",
    auth_identity_conflict: "账号验证遇到冲突，请联系网站管理员。",
    auth_invalid_origin: "登录请求已失效，请刷新页面后重试。",
  } as Record<string, string>)[code] ?? "登录未完成，请稍后重试。"
}
