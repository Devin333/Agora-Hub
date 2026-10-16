import { describe, expect, it, vi } from "vitest"
import { RuntimeCoordinator, UnavailableRuntimeController, runtimeProtocol } from "../src/runtime-coordinator"
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

function runtimeClient(invoke: ReturnType<typeof vi.fn>, cancelStart = vi.fn().mockResolvedValue(true)) {
  return { invoke, cancelStart } as unknown as RuntimeClient
}

function degradedStatus(data: Record<string, unknown> = {}) {
  return {
    ...result("status", "DEGRADED", true, ownedData({ reason_code: "desktop_degraded", health: HEALTH, ...data })),
    code: "desktop_degraded",
    message: "local desktop health was checked"
  }
}

describe("runtime coordinator", () => {
  it("does not advertise a retry when the unavailable controller cannot perform one", async () => {
    const coordinator = new UnavailableRuntimeController(new Error("runtime missing"), "1.0.0", false)
    expect(coordinator.status).toMatchObject({ phase: "unknown", canRetry: false })
    await expect(coordinator.retry()).resolves.toMatchObject({ phase: "unknown", canRetry: false })
  })

  it("checks status once and does not issue a duplicate start when already ready", async () => {
    const invoke = vi.fn().mockResolvedValue(result("status", "READY", true, ownedData()))
    const coordinator = new RuntimeCoordinator(runtimeClient(invoke), "1.0.0", false)
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
    const coordinator = new RuntimeCoordinator(runtimeClient(invoke), "1.0.0", false)
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
    const coordinator = new RuntimeCoordinator(runtimeClient(invoke), "1.0.0", false)

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
    const coordinator = new RuntimeCoordinator(runtimeClient(invoke), "1.0.0", false)

    await expect(coordinator.initialize()).resolves.toMatchObject({ phase: "degraded", state: "DRAINING", localTransportReady: false })
    expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "start", "status"])
  })
  it("cooperatively cancels an in-flight start and still performs a real verified stop", async () => {
    let resolveStart: ((value: ReturnType<typeof result>) => void) | undefined
    const cancelStart = vi.fn().mockResolvedValue(true)
    const invoke = vi.fn()
      .mockResolvedValueOnce(result("status", "STOPPED"))
      .mockImplementationOnce(() => new Promise(resolve => { resolveStart = resolve }))
      .mockResolvedValueOnce(result("stop", "STOPPED"))
    const coordinator = new RuntimeCoordinator(runtimeClient(invoke, cancelStart), "1.0.0", false)
    const starting = coordinator.initialize()
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(2))
    const stopping = coordinator.stop()
    expect(coordinator.status).toMatchObject({ phase: "starting", state: "STOPPING", code: "runtime_start_cancelling" })
    expect(cancelStart).toHaveBeenCalledOnce()
    resolveStart?.({ ...result("start", "ERROR", false), code: "desktop_start_cancelled" })
    await starting
    await expect(stopping).resolves.toMatchObject({ phase: "stopped", localTransportReady: false })
    expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "start", "stop"])
  })

  it("fences a pending status so close cannot launch start after STOPPED is observed", async () => {
    let resolveStatus: ((value: ReturnType<typeof result>) => void) | undefined
    const invoke = vi.fn()
      .mockImplementationOnce(() => new Promise(resolve => { resolveStatus = resolve }))
      .mockResolvedValueOnce(result("stop", "STOPPED"))
    const coordinator = new RuntimeCoordinator(runtimeClient(invoke), "1.0.0", false)
    const initializing = coordinator.initialize()
    await vi.waitFor(() => expect(invoke).toHaveBeenCalledTimes(1))
    const stopping = coordinator.stop()
    resolveStatus?.(result("status", "STOPPED"))
    await initializing
    await expect(stopping).resolves.toMatchObject({ phase: "stopped" })
    expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "stop"])
  })

  it("keeps UNKNOWN and does not race stop when startup cancellation is not confirmed", async () => {
    vi.useFakeTimers()
    try {
      let resolveStart: ((value: ReturnType<typeof result>) => void) | undefined
      const invoke = vi.fn()
        .mockResolvedValueOnce(result("status", "STOPPED"))
        .mockImplementationOnce(() => new Promise(resolve => { resolveStart = resolve }))
        .mockResolvedValueOnce(result("stop", "STOPPED"))
      const cancelStart = vi.fn().mockResolvedValue(true)
      const coordinator = new RuntimeCoordinator(runtimeClient(invoke, cancelStart), "1.0.0", false, { startCancellationTimeoutMs: 100 })
      const starting = coordinator.initialize()
      await vi.advanceTimersByTimeAsync(1)
      const firstStop = coordinator.stop()
      const secondStop = coordinator.stop()
      expect(firstStop).toBe(secondStop)
      await vi.advanceTimersByTimeAsync(101)
      await expect(firstStop).resolves.toMatchObject({
        phase: "unknown", state: "UNKNOWN", code: "runtime_start_cancel_unconfirmed", canRetry: false
      })
      expect(cancelStart).toHaveBeenCalledOnce()
      expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "start"])
      expect(coordinator.diagnostics.runtime).toMatchObject({ phase: "unknown", code: "runtime_start_cancel_unconfirmed", canRetry: false })
      await expect(coordinator.retry()).resolves.toMatchObject({ code: "runtime_start_cancel_unconfirmed", canRetry: false })
      expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "start"])
      resolveStart?.({ ...result("start", "ERROR", false), code: "desktop_start_cancelled" })
      await starting
      await expect(coordinator.stop()).resolves.toMatchObject({ phase: "stopped" })
      expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "start", "stop"])
    } finally {
      vi.useRealTimers()
    }
  })

  it("does not turn invalid JSON or secret-bearing diagnostics into successful state", async () => {
    const invoke = vi.fn()
      .mockRejectedValueOnce(new Error("runtime_protocol_invalid_json Authorization: Bearer top-secret-value"))
      .mockResolvedValueOnce(result("stop", "STOPPED"))
    const coordinator = new RuntimeCoordinator(runtimeClient(invoke), "1.0.0", false)
    await expect(coordinator.initialize()).resolves.toMatchObject({ phase: "unknown", state: "UNKNOWN" })
    expect(JSON.stringify(coordinator.diagnostics)).not.toContain("top-secret-value")
    await expect(coordinator.stop()).resolves.toMatchObject({ phase: "stopped" })
    expect(invoke.mock.calls.map(call => call[0])).toEqual(["status", "stop"])
  })

  it("reports finite startup verification as starting while the owner command remains in flight", async () => {
    let resolveStart: ((value: ReturnType<typeof result>) => void) | undefined
    const invoke = vi.fn()
      .mockResolvedValueOnce(result("status", "STOPPED"))
      .mockImplementationOnce(() => new Promise(resolve => { resolveStart = resolve }))
    const coordinator = new RuntimeCoordinator(runtimeClient(invoke), "1.0.0", false)
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

