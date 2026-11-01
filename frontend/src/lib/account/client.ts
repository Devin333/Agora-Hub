import { z } from "zod"

// Browser client for the hosted account service. Every call goes through the
// audited BFF: /api/v1/* is the allow-listed proxy, and the password change
// uses /api/auth/password because it rotates the session cookie.

const accountRoleSchema = z.enum(["admin", "user"])
export type AccountRole = z.infer<typeof accountRoleSchema>

export const accountSchema = z.object({
  userId: z.string().min(1),
  username: z.string().min(1),
  role: accountRoleSchema,
  enabled: z.boolean(),
  createdAt: z.string().nullable().optional(),
  updatedAt: z.string().nullable().optional(),
  disabledAt: z.string().nullable().optional(),
  passwordChangedAt: z.string().nullable().optional(),
}).strip()
export type Account = z.infer<typeof accountSchema>

const accountPayloadSchema = z.object({ account: accountSchema }).strip()
const accountListSchema = z.object({ accounts: z.array(accountSchema) }).strip()
const accountWithPasswordSchema = z.object({
  account: accountSchema,
  initialPassword: z.string().min(1).optional(),
}).strip()
export type AccountWithPassword = z.infer<typeof accountWithPasswordSchema>

export type AccountErrorDetails = Record<string, unknown>

export class AccountServiceError extends Error {
  constructor(
    public code: string,
    message: string,
    public status?: number,
    public details: AccountErrorDetails = {},
  ) {
    super(message)
    this.name = "AccountServiceError"
  }
}

type Envelope = {
  success?: boolean
  data?: unknown
  error?: { code?: string; message?: string; details?: AccountErrorDetails | null } | null
}

export async function accountRequest<T>(schema: z.ZodType<T>, path: string, init: RequestInit = {}): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, {
      ...init,
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
        ...init.headers,
      },
    })
  } catch {
    throw new AccountServiceError("account_service_unavailable", accountErrorMessage("account_service_unavailable"))
  }
  const envelope = (await response.json().catch(() => null)) as Envelope | null
  if (!response.ok || envelope?.success !== true) {
    const code = envelope?.error?.code ?? `http_${response.status}`
    const details = envelope?.error?.details ?? {}
    throw new AccountServiceError(code, accountErrorMessage(code, details), response.status, details)
  }
  const parsed = schema.safeParse(envelope.data)
  if (!parsed.success) {
    throw new AccountServiceError("account_response_invalid", accountErrorMessage("account_response_invalid"), response.status)
  }
  return parsed.data
}

export function jsonBody(method: string, body: Record<string, unknown>, signal?: AbortSignal): RequestInit {
  return { method, body: JSON.stringify(body), signal }
}

function adminAccountPath(userId: string, action: "disable" | "enable" | "password" | "role") {
  return `/api/v1/admin/accounts/${encodeURIComponent(userId)}/${action}`
}

export async function fetchOwnAccount(signal?: AbortSignal): Promise<Account> {
  return (await accountRequest(accountPayloadSchema, "/api/v1/account", { signal })).account
}

export async function changeOwnPassword(currentPassword: string, newPassword: string): Promise<void> {
  // The BFF swaps in the fresh session cookie; the signed-in user is unchanged,
  // so the open workspace keeps running while every other session is revoked.
  await accountRequest(z.object({ session: z.unknown() }).strip(), "/api/auth/password", jsonBody("POST", { currentPassword, newPassword }))
}

export async function listAccounts(signal?: AbortSignal): Promise<Account[]> {
  return (await accountRequest(accountListSchema, "/api/v1/admin/accounts", { signal })).accounts
}

export function createAccount(input: { username: string; role: AccountRole; password?: string }): Promise<AccountWithPassword> {
  const body: Record<string, unknown> = { username: input.username, role: input.role }
  if (input.password) body.password = input.password
  return accountRequest(accountWithPasswordSchema, "/api/v1/admin/accounts", jsonBody("POST", body))
}

export async function setAccountEnabled(userId: string, enabled: boolean): Promise<Account> {
  return (await accountRequest(accountPayloadSchema, adminAccountPath(userId, enabled ? "enable" : "disable"), jsonBody("POST", {}))).account
}

export function resetAccountPassword(userId: string, password?: string): Promise<AccountWithPassword> {
  return accountRequest(accountWithPasswordSchema, adminAccountPath(userId, "password"), jsonBody("POST", password ? { password } : {}))
}

export async function changeAccountRole(userId: string, role: AccountRole): Promise<Account> {
  return (await accountRequest(accountPayloadSchema, adminAccountPath(userId, "role"), jsonBody("PUT", { role }))).account
}

const ACCOUNT_ERROR_MESSAGES: Record<string, string> = {
  account_service_unavailable: "账号服务暂时无法连接，请检查网络后重试。",
  account_response_invalid: "账号服务返回了无法识别的数据，请稍后重试。",
  auth_required: "登录已失效，请重新登录。",
  auth_session_invalid: "登录已失效，请重新登录。",
  auth_invalid_origin: "请求来源无效，请刷新页面后重试。",
  auth_invalid_credentials: "当前密码不正确。",
  auth_invalid_request: "请检查输入：新密码至少 8 位。",
  account_invalid_request: "输入不符合要求：用户名 3–64 位，密码至少 8 位。",
  account_exists: "该用户名已被使用。",
  account_not_found: "账号不存在或已被删除。",
  account_disabled: "该账号已被停用。",
  last_admin_required: "至少需要保留一个启用中的管理员。",
  cannot_disable_self: "不能停用自己正在使用的账号。",
  model_connection_required: "请先在设置里为这个用途分配一个模型连接。",
  model_connection_key_required: "请填写 API Key：新连接、换了服务商或地址时都需要重新填写。",
  model_connection_key_missing: "这个连接的 API Key 需要重新填写。",
  model_connection_key_rotated: "服务器的加密密钥已更换，请重新填写这个连接的 API Key。",
  model_connection_withdrawn: "这个连接所用的服务商或模型已被管理员下线，请改用其他模型。",
  model_connection_rejected: "模型服务拒绝了这个 API Key，请检查后重试。",
  model_connection_model_unavailable: "这个 API Key 有效，但模型服务没有向它开放所选模型，请换一个模型或检查 Key 的权限。",
  model_connection_not_found: "这个模型连接不存在或已被删除。",
  model_connection_limit_reached: "模型连接数量已达上限，请先删除不用的连接。",
  model_connection_invalid: "输入不符合要求，请检查后重试。",
  model_connection_conflict: "模型设置已在其他窗口被修改，已为你刷新，请确认后再保存。",
  model_connection_window_too_small: "这个模型的上下文窗口不够大，不能用于这个用途。",
  model_connection_structured_output_required: "这个用途需要模型能返回 JSON 对象，请换一个支持结构化输出的模型。",
  model_discovery_unsupported: "这个地址不提供模型列表，请手动填写模型 ID。",
  model_provider_unreachable: "暂时无法连接模型服务，请稍后重试。",
  model_provider_unsupported: "不支持所选的模型服务商或模型。",
  model_endpoint_not_allowed: "这个模型地址不被允许：只能用公网 HTTPS 地址，内网地址需要管理员加入白名单。",
  model_probe_rate_limited: "验证和获取模型列表过于频繁，请稍后再试。",
  http_403: "没有权限执行该操作。",
  http_404: "该功能当前不可用。",
  http_429: "操作过于频繁，请稍后重试。",
}

// Which form field a model_connection_invalid error names (interfaces/services/model_connections/drafts.py).
const INVALID_FIELD_MESSAGES: Record<string, string> = {
  apiKey: "API Key 需要是 8–512 个可见的英文字符，不能有空格。",
  baseUrl: "请填写完整的服务地址，例如 https://api.example.com/v1，不能带用户名、密码、查询参数。",
  model: "模型 ID 只能包含字母、数字和 . _ : / @ -，最长 128 个字符。",
  contextWindowTokens: "请填写上下文窗口大小（1,024 到 20,000,000 之间的整数）。",
  maxOutputTokens: "最大输出必须是正整数，并且小于上下文窗口。",
  reasoningParameter: "这个服务商不支持所选的思考参数格式。",
  reasoningEffort: "这个模型不支持所选的思考强度。",
  structuredOutput: "这个服务商不支持所选的结构化输出方式。",
  label: "名称最多 64 个字符。",
  assignments: "用途不正确，请刷新页面后重试。",
}

export function accountErrorMessage(code: string, details: AccountErrorDetails = {}): string {
  const purpose = typeof details.purposeLabel === "string" ? details.purposeLabel : null
  if (code === "model_connection_window_too_small" && purpose) {
    const required = formatTokens(details.requiredWindow)
    const actual = formatTokens(details.contextWindow)
    if (required && actual) {
      return `“${purpose}”需要至少 ${required} 的上下文窗口，这个模型只有 ${actual}。请换一个窗口更大的模型。`
    }
  }
  if (code === "model_connection_structured_output_required" && purpose) {
    return `“${purpose}”需要模型能返回 JSON 对象，请换一个支持结构化输出的模型。`
  }
  if (code === "model_connection_required" && purpose) {
    return `请先在设置里为“${purpose}”分配一个模型连接。`
  }
  if (code === "model_connection_invalid" && typeof details.field === "string" && details.field in INVALID_FIELD_MESSAGES) {
    return INVALID_FIELD_MESSAGES[details.field]
  }
  if (code === "model_connection_limit_reached" && typeof details.limit === "number") {
    return `每个账号最多保存 ${details.limit} 个模型连接，请先删除不用的连接。`
  }
  return ACCOUNT_ERROR_MESSAGES[code] ?? "操作未完成，请稍后重试。"
}

/** Token counts as people read them: 1,000,000 → "100 万 token". */
export function formatTokens(value: unknown): string | null {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return null
  if (value >= 10_000) {
    const wan = value / 10_000
    return `${Number.isInteger(wan) ? wan : wan.toFixed(1)} 万 token`
  }
  return `${value.toLocaleString("zh-CN")} token`
}
