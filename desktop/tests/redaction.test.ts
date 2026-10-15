import { describe, expect, it } from "vitest"
import { redactRecord, redactText } from "../src/redaction"

describe("diagnostic redaction", () => {
  it("removes keys, bearer values, and user profile prefixes", () => {
    expect(redactRecord({ api_key: "secret", nested: { token: "abc", safe: "ok" } })).toEqual({ api_key: "[REDACTED]", nested: { token: "[REDACTED]", safe: "ok" } })
    expect(redactText("Bearer abcdefghijklmnop C:\\Users\\Alice\\file.txt")).not.toContain("abcdefghijklmnop")
    expect(redactText("C:\\Users\\Alice\\file.txt")).toContain("%USERPROFILE%")
  })
})
