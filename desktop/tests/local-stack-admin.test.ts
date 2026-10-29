import { describe, expect, it } from "vitest"
import { accountCount, adminFailure, createAdminArguments, validateLocalAdmin } from "../src/local-stack-admin"

describe("local administrator", () => {
  it("accepts what the server accepts", () => {
    expect(validateLocalAdmin({ username: " owner ", password: "local-pass", confirmation: "local-pass" })).toEqual({ ok: true, input: { username: "owner", password: "local-pass" } })
  })

  it.each([
    [{ username: "ab", password: "local-pass", confirmation: "local-pass" }, "username_invalid"],
    [{ username: "张三", password: "local-pass", confirmation: "local-pass" }, "username_invalid"],
    [{ username: "owner", password: "short", confirmation: "short" }, "password_too_short"],
    [{ username: "owner", password: "x".repeat(257), confirmation: "x".repeat(257) }, "password_too_long"],
    [{ username: "owner", password: "local-pass\nnext", confirmation: "local-pass\nnext" }, "password_invalid"],
    [{ username: "owner", password: "local-pass", confirmation: "local-pas" }, "password_mismatch"],
    [null, "username_invalid"]
  ])("refuses %j with %s", (value, code) => {
    expect(validateLocalAdmin(value)).toMatchObject({ ok: false, code })
  })

  it("never puts the password on the command line", () => {
    expect(createAdminArguments("owner")).toEqual(["news", "accounts", "create-admin", "owner", "--password-stdin"])
  })

  it("reads the account list and the CLI's error document", () => {
    expect(accountCount('{"accounts": []}\n')).toBe(0)
    expect(accountCount('{"accounts": [{"username": "owner"}]}')).toBe(1)
    expect(accountCount("Traceback (most recent call last)")).toBeNull()
    expect(adminFailure('{"error": {"code": "account_exists", "message": "an account with this username already exists"}}')).toMatchObject({ code: "account_exists" })
    expect(adminFailure('warning\n{"error": {"code": "account_invalid_request", "message": "password must be at least 8 characters"}}')).toMatchObject({ code: "password_too_short" })
    expect(adminFailure("OCI runtime exec failed")).toMatchObject({ code: "admin_failed" })
  })
})
