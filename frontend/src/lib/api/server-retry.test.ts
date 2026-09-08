import { afterEach, describe, expect, it, vi } from "vitest"
import { safeApiPost } from "./server"

describe("upstream auth retry metadata", () => {
  afterEach(() => vi.unstubAllGlobals())
  it("preserves a real rate-limit window without exposing upstream details", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ success: false, error: { code: "auth_rate_limited", message: "Limited", details: { retryAfter: 3600, privateData: "secret" } } }, { status: 429 })))
    const result = await safeApiPost("/api/v1/auth/otp/challenges", {})
    expect(result).toMatchObject({ ok: false, errorCode: "auth_rate_limited", retryAfter: 3600 })
    expect(JSON.stringify(result)).not.toContain("privateData")
  })
  it.each([-1, "secret", null])("ignores invalid retry metadata: %s", async (retryAfter) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ success: false, error: { code: "auth_rate_limited", message: "Limited", details: { retryAfter } } }, { status: 429 })))
    expect(await safeApiPost("/api/v1/auth/otp/challenges", {})).not.toHaveProperty("retryAfter")
  })
})
