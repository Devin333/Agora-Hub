import { describe, expect, it, vi } from "vitest"
import { requestSettingsModalClose } from "./paper-agent-app"

describe("settings modal App lifecycle", () => {
  it("asks once and preserves the open dirty draft when discard is cancelled", () => {
    const close = vi.fn()
    const confirmDiscard = vi.fn(() => false)

    expect(requestSettingsModalClose(true, close, confirmDiscard)).toBe(false)
    expect(confirmDiscard).toHaveBeenCalledTimes(1)
    expect(close).not.toHaveBeenCalled()
  })

  it("closes and clears through one callback after discard is confirmed", () => {
    let open = true
    let dirty = true
    const confirmDiscard = vi.fn(() => true)

    expect(requestSettingsModalClose(dirty, () => { open = false; dirty = false }, confirmDiscard)).toBe(true)
    expect(confirmDiscard).toHaveBeenCalledTimes(1)
    expect(open).toBe(false)
    expect(dirty).toBe(false)
  })

  it("does not show a confirmation for a clean settings modal", () => {
    const close = vi.fn()
    const confirmDiscard = vi.fn(() => false)

    expect(requestSettingsModalClose(false, close, confirmDiscard)).toBe(true)
    expect(confirmDiscard).not.toHaveBeenCalled()
    expect(close).toHaveBeenCalledTimes(1)
  })
})
