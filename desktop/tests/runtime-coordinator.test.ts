import { describe, expect, it, vi } from "vitest"
import { RuntimeCoordinator, runtimeProtocol } from "../src/runtime-coordinator"
import type { RuntimeClient } from "../src/runtime-client"

const ORIGIN = "http://127.0.0.1:3120"
const INSTANCE_KEY = `local-desktop:${ORIGIN}`
const HEALTH = { api: true, frontend: true, bff: true, raw_rewrite: true, runtime: true }

function result(command: "status" | "start" | "stop", state: string, ok = true, data: Record<string, unknown> = {}) {
  return { ok, command, state, code: `runtime_${state.toLowerCase()}`, message: state, data }
}

function ownedData(data: Record<string, unknown> = {}) {
  return { canonical_origin: ORIGIN, instance_key: INSTANCE_KEY, ...data }
}

function degradedStatus(data: Record<string, unknown> = {}) {
  return {
    ...result("status", "DEGRADED", true, ownedData({ reason_code: "desktop_degraded", health: HEALTH, ...data })),
    code: "desktop_degraded",
    message: "local desktop health was checked"
  }
}

describe("runtime coordinator", () => {
  it("checks status once and does not issue a duplicate start when already ready", async () => {
    const invoke = vi.fn().mockResolvedValue(result("status", "READY", true, ownedData()))
    const coordinator = new RuntimeCoordinator({ invoke } as unknown as RuntimeClient, "1.0.0", false)
    await expect(Promise.all([coordinator.initialize(), coordinator.initialize()])).resolves.toEqual([
      expect.objectContaining({ phase: "ready", localTransportReady: true }),
      expect.objectContaining({ phase: "ready", localTransportReady: true })
    ])
    expect(invoke).toHaveBeenCalledTimes(1)
    expect(invoke).toHaveBeenCalledWith("status")
  })

  it("starts only after an owned status reports stopped", async () => {
    const invoke = vi.fn()
      .mockResolvedValueOnce(result("status", "STOPPED"))
      .mockResolvedValueOnce(result("start", "READY", true, ownedData()))
    const coordinator = new RuntimeCoordinator({ invoke } as unknown as RuntimeClient, "1.0.0", false)
    await expect(coordinator.initialize()).resolves.toMatchObject({ phase: "ready", localTransportReady: true })
    expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "start"])
  })

  it("rechecks owner status after a degraded start before exposing local entry", async () => {
    const startDegraded = {
      ...result("start", "DEGRADED", true, ownedData({ health: HEALTH })),
      code: "desktop_started",
      message: "local desktop transport is ready"
    }
    const invoke = vi.fn()
      .mockResolvedValueOnce(result("status", "STOPPED"))
      .mockResolvedValueOnce(startDegraded)
      .mockResolvedValueOnce(degradedStatus())
    const coordinator = new RuntimeCoordinator({ invoke } as unknown as RuntimeClient, "1.0.0", false)

    await expect(coordinator.initialize()).resolves.toMatchObject({ phase: "degraded", localTransportReady: true })
    expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "start", "status"])
  })

  it("does not expose entry when degraded startup cannot be confirmed by owner status", async () => {
    const startDegraded = {
      ...result("start", "DEGRADED", true, ownedData({ health: HEALTH })),
      code: "desktop_started",
      message: "local desktop transport is ready"
    }
    const invoke = vi.fn()
      .mockResolvedValueOnce(result("status", "STOPPED"))
      .mockResolvedValueOnce(startDegraded)
      .mockResolvedValueOnce({ ...degradedStatus(), state: "DRAINING" })
    const coordinator = new RuntimeCoordinator({ invoke } as unknown as RuntimeClient, "1.0.0", false)

    await expect(coordinator.initialize()).resolves.toMatchObject({ phase: "degraded", state: "DRAINING", localTransportReady: false })
    expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "start", "status"])
  })
  it("queues stop behind an in-flight start instead of mistaking start completion for stop", async () => {
    let resolveStart: ((value: ReturnType<typeof result>) => void) | undefined
    const invoke = vi.fn()
      .mockResolvedValueOnce(result("status", "STOPPED"))
      .mockImplementationOnce(() => new Promise(resolve => { resolveStart = resolve }))
      .mockResolvedValueOnce(result("stop", "STOPPED"))
    const coordinator = new RuntimeCoordinator({ invoke } as unknown as RuntimeClient, "1.0.0", false)
    const starting = coordinator.initialize()
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(2))
    const stopping = coordinator.stop()
    resolveStart?.(result("start", "READY", true, ownedData()))
    await expect(starting).resolves.toMatchObject({ phase: "ready" })
    await expect(stopping).resolves.toMatchObject({ phase: "stopped", localTransportReady: false })
    expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "start", "stop"])
  })

  it("reports finite startup verification as starting while the owner command remains in flight", async () => {
    let resolveStart: ((value: ReturnType<typeof result>) => void) | undefined
    const invoke = vi.fn()
      .mockResolvedValueOnce(result("status", "STOPPED"))
      .mockImplementationOnce(() => new Promise(resolve => { resolveStart = resolve }))
    const coordinator = new RuntimeCoordinator({ invoke } as unknown as RuntimeClient, "1.0.0", false)
    const starting = coordinator.initialize()
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(2))
    await expect(coordinator.refresh()).resolves.toMatchObject({
      phase: "starting",
      state: "STARTING",
      code: "runtime_start_verifying",
      localTransportReady: false,
      canRetry: false
    })
    expect(invoke).toHaveBeenCalledTimes(2)
    resolveStart?.(result("start", "READY", true, ownedData()))
    await expect(starting).resolves.toMatchObject({ phase: "ready" })
  })

  it("keeps DEGRADED truthful while allowing only a verified owned local transport", () => {
    expect(runtimeProtocol.viewFor(degradedStatus())).toMatchObject({
      phase: "degraded",
      state: "DEGRADED",
      code: "desktop_degraded",
      canonicalOrigin: ORIGIN,
      instanceKey: INSTANCE_KEY,
      localTransportReady: true,
      canRetry: true
    })
  })

  it("fails closed for draining, incomplete health, unhealthy transport, or an unverified degraded reason", () => {
    expect(runtimeProtocol.viewFor({ ...degradedStatus(), state: "DRAINING" })).toMatchObject({ phase: "degraded", localTransportReady: false })
    expect(runtimeProtocol.viewFor(degradedStatus({ health: { api: true, frontend: true, bff: true, raw_rewrite: true } }))).toMatchObject({ phase: "degraded", localTransportReady: false })
    expect(runtimeProtocol.viewFor(degradedStatus({ health: { ...HEALTH, runtime: false } }))).toMatchObject({ phase: "degraded", localTransportReady: false })
    expect(runtimeProtocol.viewFor({ ...degradedStatus(), code: "desktop_draining" })).toMatchObject({ phase: "degraded", localTransportReady: false })
  })

  it("rejects external origins and missing or mismatched local instance identity", () => {
    expect(runtimeProtocol.viewFor(result("status", "READY", true, { canonical_origin: "http://evil.example:3120", instance_key: "x" }))).toMatchObject({
      phase: "error", state: "INVALID_ORIGIN", code: "runtime_origin_invalid", canonicalOrigin: null, instanceKey: null, localTransportReady: false
    })
    expect(runtimeProtocol.viewFor(result("status", "READY", true, { canonical_origin: ORIGIN }))).toMatchObject({
      phase: "error", state: "INVALID_IDENTITY", code: "runtime_identity_invalid", localTransportReady: false
    })
    expect(runtimeProtocol.viewFor(degradedStatus({ instance_key: "local-desktop:http://127.0.0.1:9999" }))).toMatchObject({
      phase: "error", state: "INVALID_IDENTITY", code: "runtime_identity_invalid", localTransportReady: false
    })
    expect(runtimeProtocol.viewFor(result("status", "MYSTERY"))).toMatchObject({ phase: "unknown", localTransportReady: false })
  })
})

