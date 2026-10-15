import { describe, expect, it, vi } from "vitest"
import { NativeCredentialBridge } from "../src/native-credential-bridge"
import type { RuntimeCliResponse, RuntimeCommand } from "../src/contracts"

const ownerMetadata = {
  canonical_origin: "http://127.0.0.1:3120",
  api_base_url: "http://127.0.0.1:8120",
  instance_key: "local-desktop:http://127.0.0.1:3120",
  storage_mode: "owned-local"
}
function response(command: RuntimeCommand, state: string, data: Record<string, unknown>, ok = true): RuntimeCliResponse {
  return { ok, command, state, code: `${command}_${state.toLowerCase()}`, message: "credential protocol", data: { ...ownerMetadata, ...data } }
}

describe("NativeCredentialBridge protocol", () => {
  it("projects status to a boolean only and rejects secret-bearing or expanded payloads", async () => {
    const invoke = vi.fn().mockResolvedValue(response("credential-inspect", "CONFIGURED", { configured: true }))
    await expect(new NativeCredentialBridge({ invoke } as never).invoke("status")).resolves.toEqual({
      ok: true, code: "credential-inspect_configured", configured: true, state: "READY"
    })

    invoke.mockResolvedValue(response("credential-inspect", "CONFIGURED", { configured: true, value: "secret-must-not-cross" }))
    const invalid = await new NativeCredentialBridge({ invoke } as never).invoke("status")
    expect(invalid).toEqual({ ok: false, code: "desktop_credential_protocol_invalid", configured: null, state: "UNKNOWN" })
    expect(JSON.stringify(invalid)).not.toContain("secret-must-not-cross")

    invoke.mockResolvedValue(response("credential-inspect", "CONFIGURED", { configured: true, canonical_origin: "http://attacker.invalid" }))
    await expect(new NativeCredentialBridge({ invoke } as never).invoke("status")).resolves.toEqual({
      ok: false, code: "desktop_credential_protocol_invalid", configured: null, state: "UNKNOWN"
    })
  })

  it("requires the complete fixed owner metadata instead of accepting a payload-only reply", async () => {
    const reply = response("credential-inspect", "CONFIGURED", { configured: true })
    reply.data = { configured: true }
    const invoke = vi.fn().mockResolvedValue(reply)
    await expect(new NativeCredentialBridge({ invoke } as never).invoke("status")).resolves.toEqual({
      ok: false, code: "desktop_credential_protocol_invalid", configured: null, state: "UNKNOWN"
    })
  })

  it("projects a successful native replacement without exposing owner metadata or a secret", async () => {
    const invoke = vi.fn().mockResolvedValue(response("credential-prompt", "CONFIGURED", { configured: true, changed: true, cancelled: false }))
    await expect(new NativeCredentialBridge({ invoke } as never).invoke("replace", 42n)).resolves.toEqual({
      ok: true, code: "credential-prompt_configured", configured: true, changed: true, cancelled: false, restartRequired: true, state: "READY"
    })
  })

  it("does not fabricate configured presence when the native prompt is cancelled", async () => {
    const invoke = vi.fn().mockResolvedValue(response("credential-prompt", "CANCELLED", { configured: false, changed: false, cancelled: true }))
    const bridge = new NativeCredentialBridge({ invoke } as never)
    await expect(bridge.invoke("replace", 42n)).resolves.toEqual({
      ok: true,
      code: "credential-prompt_cancelled",
      configured: false,
      changed: false,
      cancelled: true,
      restartRequired: false,
      state: "READY"
    })
    expect(invoke).toHaveBeenCalledWith("credential-prompt", { parentWindow: 42n })
  })

  it("delegates delete to the owner command and requires a stopped lifecycle receipt", async () => {
    const invoke = vi.fn().mockResolvedValue(response("credential-delete", "UNCONFIGURED", {
      configured: false,
      lifecycle: { status: "STOPPED", reason_code: "runtime_stopped" }
    }))
    const bridge = new NativeCredentialBridge({ invoke } as never)
    await expect(bridge.invoke("delete")).resolves.toEqual({
      ok: true,
      code: "credential-delete_unconfigured",
      configured: false,
      changed: true,
      cancelled: false,
      restartRequired: true,
      state: "STOPPED"
    })
    expect(invoke).toHaveBeenCalledOnce()
    expect(invoke).toHaveBeenCalledWith("credential-delete", {})

    invoke.mockResolvedValue(response("credential-delete", "UNCONFIGURED", { configured: false }))
    await expect(bridge.invoke("delete")).resolves.toEqual({ ok: false, code: "desktop_credential_protocol_invalid", configured: null, state: "UNKNOWN" })
  })

  it("maps owner/API failures to UNKNOWN instead of preserving an authoritative state", async () => {
    const invoke = vi.fn().mockResolvedValue(response("credential-inspect", "CONFIGURED", { configured: true }, false))
    await expect(new NativeCredentialBridge({ invoke } as never).invoke("status")).resolves.toEqual({
      ok: false, code: "credential-inspect_configured", configured: null, state: "UNKNOWN"
    })
  })
})
