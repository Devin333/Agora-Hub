import { describe, expect, it } from "vitest"
import { attachFrontendRuntimeIdentity, frontendRuntimeIdentity } from "./runtime-identity"

const valid: NodeJS.ProcessEnv = {
  NODE_ENV: "test",
  NEWSROOM_DEPLOYMENT_PROFILE: "local-desktop",
  NEWSROOM_DESKTOP_INSTANCE_KEY: "local-desktop:http://127.0.0.1:3000",
  NEWSROOM_DESKTOP_RELEASE_ID: "release-1",
  NEWSROOM_DESKTOP_CONFIG_DIGEST: "a".repeat(64),
  NEWSROOM_DESKTOP_BOOT_ID: "b".repeat(32),
}

describe("frontend runtime identity", () => {
  it("echoes only the server runtime identity in local desktop", () => {
    expect(attachFrontendRuntimeIdentity({ session: null }, valid)).toEqual({
      session: null,
      local_desktop: {
        deployment_profile: "local-desktop",
        instance_key: "local-desktop:http://127.0.0.1:3000",
        release_id: "release-1",
        config_digest: "a".repeat(64),
        boot_id: "b".repeat(32),
      },
    })
  })

  it("replaces an upstream identity claim with the frontend server identity", () => {
    expect(attachFrontendRuntimeIdentity({
      session: null,
      local_desktop: {
        deployment_profile: "local-desktop",
        instance_key: "local-desktop:http://127.0.0.1:3000",
        release_id: "stale-release",
        config_digest: "c".repeat(64),
        boot_id: "d".repeat(32),
      },
    }, valid)).toMatchObject({
      local_desktop: { boot_id: "b".repeat(32), config_digest: "a".repeat(64) },
    })
  })

  it("leaves default profile data unchanged", () => {
    expect(attachFrontendRuntimeIdentity({ session: null }, {
      NODE_ENV: "test",
      NEWSROOM_DEPLOYMENT_PROFILE: "default",
    })).toEqual({ session: null })
  })

  it("rejects missing or stale-shaped boot and config identity", () => {
    expect(() => frontendRuntimeIdentity({ ...valid, NEWSROOM_DESKTOP_BOOT_ID: "" })).toThrow()
    expect(() => frontendRuntimeIdentity({ ...valid, NEWSROOM_DESKTOP_CONFIG_DIGEST: "stale" })).toThrow()
    expect(() => frontendRuntimeIdentity({ ...valid, NEWSROOM_DESKTOP_BOOT_ID: "c".repeat(31) })).toThrow()
    expect(() => frontendRuntimeIdentity({ ...valid, NEWSROOM_DEPLOYMENT_PROFILE: "desktop" })).toThrow()
  })
})
