import { afterEach, describe, expect, it, vi } from "vitest"
import {
  AccountServiceError,
  changeAccountRole,
  changeOwnPassword,
  createAccount,
  fetchOwnAccount,
  formatTokens,
  listAccounts,
  resetAccountPassword,
  setAccountEnabled,
} from "./client"

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

  it("reads the account through the same-origin BFF without caching and strips unknown fields", async () => {
    const fetch = respond({ success: true, data: { account: { ...account, passwordHash: "secret" } } })
    const read = await fetchOwnAccount()
    expect(read).toEqual(account)
    expect(read).not.toHaveProperty("passwordHash")
    const { path, init } = lastCall(fetch)
    expect(path).toBe("/api/v1/account")
    expect(init).toMatchObject({ credentials: "same-origin", cache: "no-store" })
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

  it("keeps error details and names the purpose and window a connection lacks", async () => {
    const details = { purpose: "answer", purposeLabel: "回答", requiredWindow: 343_539, contextWindow: 128_000 }
    respond({ success: false, error: { code: "model_connection_window_too_small", message: "upstream", details } }, 400)
    const error = await listAccounts().catch(cause => cause)
    expect(error).toMatchObject({ code: "model_connection_window_too_small", details })
    expect(error.message).toBe("“回答”需要至少 34.4 万 token 的上下文窗口，这个模型只有 12.8 万 token。请换一个窗口更大的模型。")

    respond({ success: false, error: { code: "model_connection_invalid", details: { field: "baseUrl" } } }, 400)
    await expect(listAccounts()).rejects.toMatchObject({ message: expect.stringContaining("https://api.example.com/v1") })

    // Without the numbers the generic sentence stays truthful.
    respond({ success: false, error: { code: "model_connection_window_too_small" } }, 400)
    await expect(listAccounts()).rejects.toMatchObject({ message: "这个模型的上下文窗口不够大，不能用于这个用途。" })
  })

  it("formats token counts the way the settings show them", () => {
    expect(formatTokens(1_000_000)).toBe("100 万 token")
    expect(formatTokens(343_539)).toBe("34.4 万 token")
    expect(formatTokens(8_000)).toBe("8,000 token")
    expect(formatTokens(0)).toBeNull()
    expect(formatTokens("1000")).toBeNull()
  })
})
