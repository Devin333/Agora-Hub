/**
 * The one server address the desktop client talks to.
 *
 * An address is reduced to a bare origin: the workspace path is fixed by the
 * client, and anything else a user might paste (a path, a query, credentials)
 * is rejected rather than silently dropped, so what is saved is exactly what
 * was confirmed. The hosted service issues `Secure` session cookies, so only
 * HTTPS works against a real server; plain HTTP is accepted for a loopback
 * host in an unpackaged build, where a developer runs the service locally.
 */
export interface ServerOriginPolicy {
  allowLoopbackHttp: boolean
}

export type ServerOriginErrorCode =
  | "server_url_empty"
  | "server_url_invalid"
  | "server_url_scheme_unsupported"
  | "server_url_insecure"
  | "server_url_has_credentials"
  | "server_url_has_path"

export type ServerOriginResult = { ok: true; origin: string } | { ok: false; code: ServerOriginErrorCode }

export const SERVER_ORIGIN_ERROR_MESSAGES: Readonly<Record<ServerOriginErrorCode, string>> = Object.freeze({
  server_url_empty: "请输入服务器地址。",
  server_url_invalid: "这不是有效的服务器地址，例如：https://agora.example.com",
  server_url_scheme_unsupported: "服务器地址只能以 https:// 开头。",
  server_url_insecure: "服务器必须使用 HTTPS。请让管理员为服务器配置域名和证书。",
  server_url_has_credentials: "服务器地址里不能包含用户名或密码。",
  server_url_has_path: "只需要填写服务器地址本身，不要包含路径、参数或 # 片段。"
})

const MAX_ADDRESS_LENGTH = 2048
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"])

export function isLoopbackHost(hostname: string): boolean {
  return LOOPBACK_HOSTS.has(hostname.toLowerCase())
}

export function parseServerOrigin(raw: unknown, policy: ServerOriginPolicy): ServerOriginResult {
  if (typeof raw !== "string") return { ok: false, code: "server_url_invalid" }
  const trimmed = raw.trim()
  if (!trimmed) return { ok: false, code: "server_url_empty" }
  if (trimmed.length > MAX_ADDRESS_LENGTH || /[\s\0]/.test(trimmed)) return { ok: false, code: "server_url_invalid" }
  // A bare host name ("agora.example.com") means HTTPS; an explicit scheme is kept as typed.
  const candidate = /^[a-z][a-z0-9+.-]*:/i.test(trimmed) && !/^[^/]*:\d+(\/|$)/.test(trimmed) ? trimmed : `https://${trimmed}`
  let url: URL
  try {
    url = new URL(candidate)
  } catch {
    return { ok: false, code: "server_url_invalid" }
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return { ok: false, code: "server_url_scheme_unsupported" }
  if (!url.hostname) return { ok: false, code: "server_url_invalid" }
  if (url.username || url.password) return { ok: false, code: "server_url_has_credentials" }
  if (url.pathname !== "/" || url.search || url.hash || candidate.endsWith("?") || candidate.endsWith("#")) {
    return { ok: false, code: "server_url_has_path" }
  }
  if (url.protocol === "http:" && !(policy.allowLoopbackHttp && isLoopbackHost(url.hostname))) {
    return { ok: false, code: "server_url_insecure" }
  }
  return { ok: true, origin: url.origin }
}

export function serverOriginErrorMessage(code: ServerOriginErrorCode): string {
  return SERVER_ORIGIN_ERROR_MESSAGES[code]
}
