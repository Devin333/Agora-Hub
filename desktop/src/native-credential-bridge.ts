import type { DesktopCredentialResult, RuntimeCommand } from "./contracts"
import { RuntimeClient, RuntimeInvocationError } from "./runtime-client"

export type CredentialCommand = "status" | "replace" | "delete"
export interface CredentialController { invoke(command: CredentialCommand, parentWindow?: bigint): Promise<DesktopCredentialResult> }
export class UnavailableCredentialBridge implements CredentialController {
  invoke(): Promise<DesktopCredentialResult> { return Promise.resolve(unknown("desktop_credential_bridge_unavailable")) }
}
function unknown(code: string): DesktopCredentialResult { return { ok: false, code, configured: null, state: "UNKNOWN" } }
const COMMANDS: Record<CredentialCommand, RuntimeCommand> = { status: "credential-inspect", replace: "credential-prompt", delete: "credential-delete" }
const OWNER_METADATA = Object.freeze({
  canonical_origin: "http://127.0.0.1:3120",
  api_base_url: "http://127.0.0.1:8120",
  instance_key: "local-desktop:http://127.0.0.1:3120",
  storage_mode: "owned-local"
})
function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value).sort()
  const expected = [...keys].sort()
  return actual.length === expected.length && actual.every((key, index) => key === expected[index])
}
function hasExactOwnerEnvelope(data: Record<string, unknown>, payloadKeys: readonly string[]): boolean {
  if (!exactKeys(data, [...payloadKeys, ...Object.keys(OWNER_METADATA)])) return false
  return Object.entries(OWNER_METADATA).every(([key, value]) => data[key] === value)
}

/** Native CredUI and drain/delete both belong to the controlled Python runtime owner.
 * Neither the renderer nor this process receives the vault value. The shared client
 * validates exit status and joins timed-out children before returning UNKNOWN.
 */
export class NativeCredentialBridge implements CredentialController {
  constructor(private readonly client: RuntimeClient) {}
  async invoke(command: CredentialCommand, parentWindow?: bigint): Promise<DesktopCredentialResult> {
    try {
      const response = await this.client.invoke(COMMANDS[command], command === "replace" ? { parentWindow } : {})
      const data = response.data
      if (!response.ok) return unknown(response.code)
      if (typeof data.configured !== "boolean") return unknown("desktop_credential_protocol_invalid")
      if (command === "replace") {
        if (!hasExactOwnerEnvelope(data, ["configured", "changed", "cancelled"]) || typeof data.changed !== "boolean" || typeof data.cancelled !== "boolean" || (data.cancelled && data.changed) || (!data.cancelled && (!data.changed || !data.configured)) || response.state !== (data.cancelled ? "CANCELLED" : "CONFIGURED")) return unknown("desktop_credential_protocol_invalid")
        return { ok: true, code: response.code, configured: data.configured, changed: data.changed, cancelled: data.cancelled, restartRequired: data.changed, state: "READY" }
      }
      if (command === "delete") {
        const lifecycle = data.lifecycle
        if (!hasExactOwnerEnvelope(data, ["configured", "lifecycle"]) || data.configured || response.state !== "UNCONFIGURED" || !lifecycle || typeof lifecycle !== "object" || Array.isArray(lifecycle) || (lifecycle as Record<string, unknown>).status !== "STOPPED") return unknown("desktop_credential_protocol_invalid")
        return { ok: true, code: response.code, configured: false, changed: true, cancelled: false, restartRequired: true, state: "STOPPED" }
      }
      if (!hasExactOwnerEnvelope(data, ["configured"]) || response.state !== (data.configured ? "CONFIGURED" : "UNCONFIGURED")) return unknown("desktop_credential_protocol_invalid")
      return { ok: true, code: response.code, configured: data.configured, state: "READY" }
    } catch (error) { return unknown(error instanceof RuntimeInvocationError ? error.code : "desktop_credential_process_failed") }
  }
}
