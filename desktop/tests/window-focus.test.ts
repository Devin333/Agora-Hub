import { describe, expect, it, vi } from "vitest"
import { focusExistingWindow } from "../src/window-focus"

describe("single instance focus behavior", () => {
  it("restores and focuses the existing native window", () => {
    const window = { isMinimized: () => true, restore: vi.fn(), show: vi.fn(), focus: vi.fn() }
    focusExistingWindow(window)
    expect(window.restore).toHaveBeenCalledOnce()
    expect(window.show).toHaveBeenCalledOnce()
    expect(window.focus).toHaveBeenCalledOnce()
  })
})
