import { describe, expect, it } from "vitest"
import { desktopStackIdentity, desktopStackToken } from "./desktop-stack"

const token = "a".repeat(43)

describe("desktop stack identity", () => {
  it("answers the caller holding the stack's token with the image release", () => {
    expect(desktopStackIdentity(token, { AGORA_DESKTOP_STACK_TOKEN: token, AGORA_IMAGE_TAG: "v0.1.2" })).toEqual({ ok: true, imageTag: "v0.1.2" })
  })

  it("reports dev for an image built without a release name", () => {
    expect(desktopStackIdentity(token, { AGORA_DESKTOP_STACK_TOKEN: token })).toEqual({ ok: true, imageTag: "dev" })
  })

  it("refuses a wrong, missing or prefix token", () => {
    const env = { AGORA_DESKTOP_STACK_TOKEN: token }
    expect(desktopStackIdentity("b".repeat(43), env)).toBeNull()
    expect(desktopStackIdentity(null, env)).toBeNull()
    expect(desktopStackIdentity("", env)).toBeNull()
    expect(desktopStackIdentity(token.slice(0, 42), env)).toBeNull()
  })

  it("never answers on a server that is not a desktop-managed stack", () => {
    expect(desktopStackToken({})).toBeNull()
    expect(desktopStackIdentity(token, {})).toBeNull()
  })

  it("ignores a token too short to be safe", () => {
    expect(desktopStackToken({ AGORA_DESKTOP_STACK_TOKEN: "short" })).toBeNull()
    expect(desktopStackIdentity("short", { AGORA_DESKTOP_STACK_TOKEN: "short" })).toBeNull()
  })
})
