import { describe, expect, it, vi } from "vitest"
import { armOwnedDataClearFinalizer } from "../src/owned-data-clear-exit"

describe("armOwnedDataClearFinalizer", () => {
  it("flushes owned session writers before readiness without deleting browser data", async () => {
    const order: string[] = []
    const session = {
      closeAllConnections: vi.fn(async () => { order.push("connections-closed") }),
      flushStorageData: vi.fn(() => { order.push("storage-flushed") }),
      cookies: { flushStore: vi.fn(async () => { order.push("cookies-flushed") }) },
      clearCache: vi.fn(),
      clearStorageData: vi.fn()
    }
    const launch = vi.fn(async () => { order.push("finalizer-ready") })

    await armOwnedDataClearFinalizer(session, launch)

    expect(order).toEqual(["connections-closed", "storage-flushed", "cookies-flushed", "finalizer-ready"])
    expect(session.clearCache).not.toHaveBeenCalled()
    expect(session.clearStorageData).not.toHaveBeenCalled()
  })

  it("does not launch the finalizer when session flush fails", async () => {
    const launch = vi.fn()
    const session = {
      closeAllConnections: vi.fn(async () => undefined),
      flushStorageData: vi.fn(),
      cookies: { flushStore: vi.fn(async () => { throw new Error("flush failed") }) }
    }

    await expect(armOwnedDataClearFinalizer(session, launch)).rejects.toThrow("flush failed")
    expect(launch).not.toHaveBeenCalled()
  })
})
