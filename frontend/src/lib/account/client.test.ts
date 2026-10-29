import { afterEach, describe, expect, it, vi } from "vitest"
import {
  AccountServiceError,
  changeAccountRole,
  changeOwnPassword,
  createAccount,
  deleteModelCredential,
  fetchModelCredential,
  listAccounts,
  resetAccountPassword,
  saveModelCredential,
  setAccountEnabled,
} from "./client"

const credential = { configured: true, status: "unverified", providerId: "deepseek", model: "deepseek-chat", keyHint: "a1b2", revision: 1, updatedAt: null, verifiedAt: null }
const catalog = { providers: [{ id: "deepseek", label: "DeepSeek", baseUrl: "https://api.deepseek.com", defaultModel: "deepseek-chat", models: [{ id: "deepseek-chat", label: "DeepSeek Chat" }] }] }
const account = { userId: "user-bob", username: "bob", role: "user", enabled: true }

function respond(body: unknown, status = 200) {
  const fetch = vi.fn(async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }))
  vi.stubGlobal("fetch", fetch)
  return fetch
}

function lastCall(fetch: ReturnType<typeof respond>) {
  const [path, init] = fetch.mock.calls.at(-1) as unknown as [string, RequestInit]
  return { path, init, body: init.body === undefined ? undefined : JSON.parse(String(init.body)) }
}

describe("account client", () => {
  afterEach(() => vi.unstubAllGlobals())

  it("reads the credential through the same-origin BFF without caching", async () => {
    const fetch = respond({ success: true, data: { credential, catalog } })
    await expect(fetchModelCredential()).resolves.toEqual({ credential, catalog })
    const { path, init } = lastCall(fetch)
    expect(path).toBe("/api/v1/account/model-credential")
    expect(init).toMatchObject({ credentials: "same-origin", cache: "no-store" })
  })

  it("strips fields the contract does not know", async () => {
    respond({ success: true, data: { credential: { ...credential, encryptedKey: "ciphertext" }, catalog } })
    const payload = await fetchModelCredential()
    expect(payload.credential).not.toHaveProperty("encryptedKey")
  })

  it("sends the key only when one was typed and the revision only for a stored key", async () => {
    const fetch = respond({ success: true, data: { credential, catalog } })
    await saveModelCredential({ providerId: "deepseek", model: "deepseek-chat", apiKey: "sk-new", expectedRevision: null })
    expect(lastCall(fetch)).toMatchObject({ path: "/api/v1/account/model-credential", init: { method: "PUT" }, body: { providerId: "deepseek", model: "deepseek-chat", apiKey: "sk-new" } })
    await saveModelCredential({ providerId: "deepseek", model: "deepseek-chat", expectedRevision: 3 })
    expect(lastCall(fetch).body).toEqual({ providerId: "deepseek", model: "deepseek-chat", expectedRevision: 3 })
    await deleteModelCredential()
    expect(lastCall(fetch)).toMatchObject({ init: { method: "DELETE" }, body: undefined })
  })

  it("changes the password through the cookie-rotating auth route", async () => {
    const fetch = respond({ success: true, data: { session: { user: account } } })
    await changeOwnPassword("old-password", "new-password-1")
    expect(lastCall(fetch)).toMatchObject({ path: "/api/auth/password", init: { method: "POST" }, body: { currentPassword: "old-password", newPassword: "new-password-1" } })
  })

  it("addresses administrator actions by an encoded account id", async () => {
    const fetch = respond({ success: true, data: { account } })
    await setAccountEnabled("user/../bob", false)
    expect(lastCall(fetch).path).toBe("/api/v1/admin/accounts/user%2F..%2Fbob/disable")
    await setAccountEnabled("user-bob", true)
    expect(lastCall(fetch).path).toBe("/api/v1/admin/accounts/user-bob/enable")
    await changeAccountRole("user-bob", "admin")
    expect(lastCall(fetch)).toMatchObject({ path: "/api/v1/admin/accounts/user-bob/role", init: { method: "PUT" }, body: { role: "admin" } })
  })

  it("returns a generated password only when the server issued one", async () => {
    const fetch = respond({ success: true, data: { account, initialPassword: "issued-password" } })
    await expect(createAccount({ username: "bob", role: "user" })).resolves.toEqual({ account, initialPassword: "issued-password" })
    expect(lastCall(fetch).body).toEqual({ username: "bob", role: "user" })
    await resetAccountPassword("user-bob")
    expect(lastCall(fetch)).toMatchObject({ path: "/api/v1/admin/accounts/user-bob/password", body: {} })
  })

  it("maps server codes to fixed Chinese messages and never shows upstream text", async () => {
    respond({ success: false, error: { code: "last_admin_required", message: "internal detail" } }, 409)
    const error = await listAccounts().catch(cause => cause)
    expect(error).toBeInstanceOf(AccountServiceError)
    expect(error).toMatchObject({ code: "last_admin_required", status: 409, message: "至少需要保留一个启用中的管理员。" })

    respond({ success: false, error: { code: "provider_secret_detail" } }, 500)
    await expect(listAccounts()).rejects.toMatchObject({ code: "provider_secret_detail", message: "操作未完成，请稍后重试。" })
  })

  it("distinguishes an unreachable service from a malformed answer", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch") }))
    await expect(listAccounts()).rejects.toMatchObject({ code: "account_service_unavailable" })

    respond({ success: true, data: { accounts: [{ username: "missing id" }] } })
    await expect(listAccounts()).rejects.toMatchObject({ code: "account_response_invalid" })

    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>", { status: 502 })))
    await expect(listAccounts()).rejects.toMatchObject({ code: "http_502", status: 502 })
  })
})
