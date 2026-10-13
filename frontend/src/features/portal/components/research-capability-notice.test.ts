import { describe, expect, it } from "vitest"
import { capabilityState } from "./research-capability-notice"
import type { DesktopCapabilities } from "@/lib/research/desktop/contracts"

type Capability = DesktopCapabilities["capabilities"][number]

const base: Capability = {
  id: "provider",
  label: "Provider",
  required: true,
  configured: true,
  reachable: false,
  verified: false,
  verifiedAt: null,
  reasonCode: "live_probe_not_run",
  provenance: null,
  message: "Live verification has not run.",
  recovery: null,
}

describe("capabilityState", () => {
  it("keeps an unrun live probe unknown instead of claiming unreachable", () => {
    expect(capabilityState(base)).toBe("unknown")
  })

  it("reports unreachable only for a real connection failure reason", () => {
    expect(capabilityState({ ...base, reasonCode: "provider_connection_timeout" })).toBe(
      "unreachable",
    )
  })

  it("prioritizes verified and missing configuration states", () => {
    expect(capabilityState({ ...base, verified: true })).toBe("verified")
    expect(capabilityState({ ...base, configured: false })).toBe("missing")
  })
})
