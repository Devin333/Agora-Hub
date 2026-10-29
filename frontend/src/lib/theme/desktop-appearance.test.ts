import { afterEach, describe, expect, it, vi } from "vitest"
import { THEME_COLORS } from "./appearance"
import { syncDesktopAppearance } from "./desktop-appearance"

type BridgeWindow = Window & { agoraDesktop?: unknown }

afterEach(() => {
  delete (window as BridgeWindow).agoraDesktop
})

describe("syncDesktopAppearance", () => {
  it("does nothing in a plain browser without the desktop bridge", () => {
    expect(() => syncDesktopAppearance("dark")).not.toThrow()
  })

  it("reports the preference, not the resolved palette, so the host keeps following the OS", () => {
    const set = vi.fn().mockResolvedValue({ preference: "system", theme: "dark" })
    ;(window as BridgeWindow).agoraDesktop = { version: 1, appearance: { set } }
    syncDesktopAppearance("system")
    expect(set).toHaveBeenCalledWith("system")
  })

  it("tolerates an older host bridge and a rejected report", async () => {
    ;(window as BridgeWindow).agoraDesktop = { version: 1 }
    expect(() => syncDesktopAppearance("light")).not.toThrow()

    const set = vi.fn().mockRejectedValue(new Error("ipc_source_rejected"))
    ;(window as BridgeWindow).agoraDesktop = { version: 1, appearance: { set } }
    syncDesktopAppearance("light")
    await Promise.resolve()
    expect(set).toHaveBeenCalledOnce()
  })

  it("keeps the browser theme colors in step with the desktop window backgrounds", () => {
    // desktop/src/appearance.ts WINDOW_BACKGROUNDS paints the same Rakazo --background.
    expect(THEME_COLORS).toEqual({ light: "#ffffff", dark: "#161616" })
  })
})
