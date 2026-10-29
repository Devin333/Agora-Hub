import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const incoming = new Headers()
vi.mock("next/headers", () => ({ headers: async () => incoming }))

import { currentForwardedClientHeaders, forwardedChain, forwardedClientHeaders } from "@/lib/api/forwarded-client"

const saved = process.env.NEWSROOM_DEPLOYMENT_PROFILE

beforeEach(() => {
  process.env.NEWSROOM_DEPLOYMENT_PROFILE = "hosted"
  incoming.delete("x-forwarded-for")
})

afterEach(() => {
  if (saved === undefined) delete process.env.NEWSROOM_DEPLOYMENT_PROFILE
  else process.env.NEWSROOM_DEPLOYMENT_PROFILE = saved
})

describe("forwarded client address", () => {
  it("passes the proxy's address chain on in the hosted profile", () => {
    expect(forwardedClientHeaders(new Headers({ "x-forwarded-for": "203.0.113.7" }))).toEqual({ "X-Forwarded-For": "203.0.113.7" })
    expect(forwardedClientHeaders(new Headers({ "x-forwarded-for": "203.0.113.7 , 2001:db8::1" }))).toEqual({ "X-Forwarded-For": "203.0.113.7, 2001:db8::1" })
  })

  it("sends nothing when the chain is absent, malformed or oversized", () => {
    expect(forwardedClientHeaders(new Headers())).toEqual({})
    expect(forwardedChain("203.0.113.7, evil.example.com")).toBeNull()
    expect(forwardedChain("203.0.113.7,,10.0.0.1")).toBeNull()
    expect(forwardedChain(Array.from({ length: 9 }, () => "10.0.0.1").join(","))).toBeNull()
    expect(forwardedChain("1".repeat(600))).toBeNull()
  })

  it("forwards nothing outside the hosted profile", () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "default"
    expect(forwardedClientHeaders(new Headers({ "x-forwarded-for": "203.0.113.7" }))).toEqual({})
  })

  it("reads the current request's headers when no request object is at hand", async () => {
    incoming.set("x-forwarded-for", "198.51.100.4")
    await expect(currentForwardedClientHeaders()).resolves.toEqual({ "X-Forwarded-For": "198.51.100.4" })
  })
})
