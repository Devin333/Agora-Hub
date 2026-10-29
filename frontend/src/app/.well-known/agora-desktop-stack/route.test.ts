import { NextRequest } from "next/server"
import { afterEach, describe, expect, it, vi } from "vitest"
import { GET } from "./route"

const token = "k".repeat(43)

function probe(headers: Record<string, string> = {}) {
  return new NextRequest("http://127.0.0.1:45180/.well-known/agora-desktop-stack", { headers })
}

describe("GET /.well-known/agora-desktop-stack", () => {
  afterEach(() => vi.unstubAllEnvs())

  it("returns the release to the desktop app that started the stack", async () => {
    vi.stubEnv("AGORA_DESKTOP_STACK_TOKEN", token)
    vi.stubEnv("AGORA_IMAGE_TAG", "v0.1.2")
    const response = GET(probe({ "x-agora-desktop-stack-token": token }))
    expect(response.status).toBe(200)
    expect(response.headers.get("cache-control")).toBe("no-store")
    expect(await response.json()).toEqual({ ok: true, imageTag: "v0.1.2" })
  })

  it("looks like a missing route to anyone else", async () => {
    vi.stubEnv("AGORA_DESKTOP_STACK_TOKEN", token)
    for (const headers of [{}, { "x-agora-desktop-stack-token": "wrong" }]) {
      const response = GET(probe(headers))
      expect(response.status).toBe(404)
      expect(await response.text()).toBe("")
    }
  })

  it("does not answer on a server with no desktop stack token", () => {
    vi.stubEnv("AGORA_DESKTOP_STACK_TOKEN", "")
    expect(GET(probe({ "x-agora-desktop-stack-token": token })).status).toBe(404)
  })
})
