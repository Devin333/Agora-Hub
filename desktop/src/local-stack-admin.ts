/**
 * The first administrator of a stack this computer runs.
 *
 * The hosted service has no web bootstrap route, so the desktop app creates
 * the account the way a server operator does: `news accounts create-admin`
 * inside the api container, with the password on stdin and never in an
 * argument. The rules below mirror the server's, so the user sees the reason
 * in Chinese before anything runs; the server checks them again.
 */
export interface LocalAdminInput {
  username: string
  password: string
}

export type LocalAdminValidation = { ok: true; input: LocalAdminInput } | { ok: false; code: string; message: string }

const USERNAME = /^[A-Za-z0-9._-]{3,64}$/
export const MIN_PASSWORD_LENGTH = 8
export const MAX_PASSWORD_LENGTH = 256

export const ADMIN_MESSAGES = Object.freeze({
  username_invalid: "用户名需要 3 到 64 个字符，只能包含字母、数字、点、下划线和连字符。",
  password_too_short: `密码至少需要 ${MIN_PASSWORD_LENGTH} 个字符。`,
  password_too_long: `密码不能超过 ${MAX_PASSWORD_LENGTH} 个字符。`,
  password_invalid: "密码不能包含换行。",
  password_mismatch: "两次输入的密码不一致。",
  account_exists: "这个用户名已经被使用。",
  admin_failed: "创建管理员没有成功。可以展开详细输出查看原因后重试。"
})

export function validateLocalAdmin(value: unknown): LocalAdminValidation {
  if (!value || typeof value !== "object") return { ok: false, code: "username_invalid", message: ADMIN_MESSAGES.username_invalid }
  const record = value as Record<string, unknown>
  const username = typeof record.username === "string" ? record.username.trim() : ""
  const password = typeof record.password === "string" ? record.password : ""
  const confirmation = typeof record.confirmation === "string" ? record.confirmation : ""
  if (!USERNAME.test(username)) return { ok: false, code: "username_invalid", message: ADMIN_MESSAGES.username_invalid }
  // The CLI reads exactly one line; a line break would silently cut the password.
  if (/[\r\n]/.test(password)) return { ok: false, code: "password_invalid", message: ADMIN_MESSAGES.password_invalid }
  if (password.length < MIN_PASSWORD_LENGTH) return { ok: false, code: "password_too_short", message: ADMIN_MESSAGES.password_too_short }
  if (password.length > MAX_PASSWORD_LENGTH) return { ok: false, code: "password_too_long", message: ADMIN_MESSAGES.password_too_long }
  if (password !== confirmation) return { ok: false, code: "password_mismatch", message: ADMIN_MESSAGES.password_mismatch }
  return { ok: true, input: { username, password } }
}

/** The arguments after `docker compose … exec -T api`. */
export function createAdminArguments(username: string): string[] {
  return ["news", "accounts", "create-admin", username, "--password-stdin"]
}

export function listAccountsArguments(): string[] {
  return ["news", "accounts", "list"]
}

/** How many accounts `news accounts list` reported, or null when its output is not that document. */
export function accountCount(stdout: string): number | null {
  try {
    const document = JSON.parse(stdout.trim()) as { accounts?: unknown }
    return Array.isArray(document.accounts) ? document.accounts.length : null
  } catch {
    return null
  }
}

/** The CLI writes `{"error": {"code", "message"}}` to stderr; map what a user can fix. */
export function adminFailure(stderr: string): { code: string; message: string } {
  const line = stderr.trim().split(/\r?\n/).reverse().find(entry => entry.trim().startsWith("{"))
  let code = "admin_failed"
  let detail = ""
  try {
    const document = JSON.parse(line ?? "") as { error?: { code?: unknown; message?: unknown } }
    if (typeof document.error?.code === "string") code = document.error.code
    if (typeof document.error?.message === "string") detail = document.error.message
  } catch {
    // Not the CLI's own error document; the generic message stands.
  }
  if (code === "account_exists" || /already (exists|in use)/i.test(detail)) return { code: "account_exists", message: ADMIN_MESSAGES.account_exists }
  if (/username must be/i.test(detail)) return { code: "username_invalid", message: ADMIN_MESSAGES.username_invalid }
  if (/at least \d+ characters/i.test(detail)) return { code: "password_too_short", message: ADMIN_MESSAGES.password_too_short }
  if (/too long/i.test(detail)) return { code: "password_too_long", message: ADMIN_MESSAGES.password_too_long }
  return { code: "admin_failed", message: ADMIN_MESSAGES.admin_failed }
}
