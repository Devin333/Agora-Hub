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

const credentialStatusSchema = z.enum(["not_configured", "key_rotated", "unverified", "verified"])
export type ModelCredentialStatus = z.infer<typeof credentialStatusSchema>

export const modelCredentialSchema = z.object({
  configured: z.boolean(),
  status: credentialStatusSchema,
  providerId: z.string().nullable(),
  model: z.string().nullable(),
  keyHint: z.string().nullable(),
  revision: z.number().int().nonnegative().nullable(),
  updatedAt: z.string().nullable(),
  verifiedAt: z.string().nullable(),
}).strip()
export type ModelCredential = z.infer<typeof modelCredentialSchema>

const providerModelSchema = z.object({
  id: z.string().min(1),
  label: z.string().min(1),
  contextWindowTokens: z.number().int().positive().nullable().optional(),
}).strip()

const providerSchema = z.object({
  id: z.string().min(1),
  label: z.string().min(1),
  baseUrl: z.string().min(1),
  defaultModel: z.string().min(1),
  models: z.array(providerModelSchema),
}).strip()
export type ModelProvider = z.infer<typeof providerSchema>

const catalogSchema = z.object({ providers: z.array(providerSchema) }).strip()
export type ModelCatalog = z.infer<typeof catalogSchema>

const credentialPayloadSchema = z.object({ credential: modelCredentialSchema, catalog: catalogSchema }).strip()
export type ModelCredentialPayload = z.infer<typeof credentialPayloadSchema>

const accountPayloadSchema = z.object({ account: accountSchema }).strip()
const accountListSchema = z.object({ accounts: z.array(accountSchema) }).strip()
const accountWithPasswordSchema = z.object({
  account: accountSchema,
  initialPassword: z.string().min(1).optional(),
}).strip()
export type AccountWithPassword = z.infer<typeof accountWithPasswordSchema>

export class AccountServiceError extends Error {
  constructor(public code: string, message: string, public status?: number) {
    super(message)
    this.name = "AccountServiceError"
  }
}

type Envelope = { success?: boolean; data?: unknown; error?: { code?: string; message?: string } | null }

async function request<T>(schema: z.ZodType<T>, path: string, init: RequestInit = {}): Promise<T> {
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
    throw new AccountServiceError(code, accountErrorMessage(code), response.status)
  }
  const parsed = schema.safeParse(envelope.data)
  if (!parsed.success) {
    throw new AccountServiceError("account_response_invalid", accountErrorMessage("account_response_invalid"), response.status)
  }
  return parsed.data
}

function json(method: string, body: Record<string, unknown>, signal?: AbortSignal): RequestInit {
  return { method, body: JSON.stringify(body), signal }
}

function adminAccountPath(userId: string, action: "disable" | "enable" | "password" | "role") {
  return `/api/v1/admin/accounts/${encodeURIComponent(userId)}/${action}`
}

export async function fetchOwnAccount(signal?: AbortSignal): Promise<Account> {
  return (await request(accountPayloadSchema, "/api/v1/account", { signal })).account
}

export async function changeOwnPassword(currentPassword: string, newPassword: string): Promise<void> {
  // The BFF swaps in the fresh session cookie; the signed-in user is unchanged,
  // so the open workspace keeps running while every other session is revoked.
  await request(z.object({ session: z.unknown() }).strip(), "/api/auth/password", json("POST", { currentPassword, newPassword }))
}

export function fetchModelCredential(signal?: AbortSignal): Promise<ModelCredentialPayload> {
  return request(credentialPayloadSchema, "/api/v1/account/model-credential", { signal })
}

export function saveModelCredential(input: {
  providerId: string
  model: string
  apiKey?: string
  expectedRevision?: number | null
}): Promise<ModelCredentialPayload> {
  const body: Record<string, unknown> = { providerId: input.providerId, model: input.model }
  if (input.apiKey) body.apiKey = input.apiKey
  if (input.expectedRevision) body.expectedRevision = input.expectedRevision
  return request(credentialPayloadSchema, "/api/v1/account/model-credential", json("PUT", body))
}

export function verifyModelCredential(): Promise<ModelCredentialPayload> {
  return request(credentialPayloadSchema, "/api/v1/account/model-credential/verify", json("POST", {}))
}

export function deleteModelCredential(): Promise<ModelCredentialPayload> {
  return request(credentialPayloadSchema, "/api/v1/account/model-credential", { method: "DELETE" })
}

export async function listAccounts(signal?: AbortSignal): Promise<Account[]> {
  return (await request(accountListSchema, "/api/v1/admin/accounts", { signal })).accounts
}

export function createAccount(input: { username: string; role: AccountRole; password?: string }): Promise<AccountWithPassword> {
  const body: Record<string, unknown> = { username: input.username, role: input.role }
  if (input.password) body.password = input.password
  return request(accountWithPasswordSchema, "/api/v1/admin/accounts", json("POST", body))
}

export async function setAccountEnabled(userId: string, enabled: boolean): Promise<Account> {
  return (await request(accountPayloadSchema, adminAccountPath(userId, enabled ? "enable" : "disable"), json("POST", {}))).account
}

export function resetAccountPassword(userId: string, password?: string): Promise<AccountWithPassword> {
  return request(accountWithPasswordSchema, adminAccountPath(userId, "password"), json("POST", password ? { password } : {}))
}

export async function changeAccountRole(userId: string, role: AccountRole): Promise<Account> {
  return (await request(accountPayloadSchema, adminAccountPath(userId, "role"), json("PUT", { role }))).account
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
  model_credential_required: "请先在设置中填写并保存模型 API Key。",
  model_credential_key_rotated: "服务器密钥已轮换，请重新填写模型 API Key。",
  model_credential_rejected: "模型服务拒绝了这个 API Key，请检查后重试。",
  model_credential_model_unavailable: "这个 API Key 有效，但模型服务没有向它开放所选模型，请换一个模型或检查 Key 的权限。",
  model_provider_unreachable: "暂时无法连接模型服务，请稍后重试。",
  model_credential_conflict: "配置已在其他地方被修改，已为你刷新，请确认后再保存。",
  model_provider_unsupported: "不支持所选的模型服务商或模型。",
  http_403: "没有权限执行该操作。",
  http_404: "该功能当前不可用。",
  http_429: "操作过于频繁，请稍后重试。",
}

export function accountErrorMessage(code: string): string {
  return ACCOUNT_ERROR_MESSAGES[code] ?? "操作未完成，请稍后重试。"
}
