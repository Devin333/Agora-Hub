import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import {
  appearanceFilePath,
  DesktopAppearance,
  readAppearancePreference,
  validateAppearancePreference,
  windowBackgroundFor,
  type NativeThemeLike
} from "../src/appearance"

/** Mimics Electron's nativeTheme: `themeSource` drives `shouldUseDarkColors`, "system" follows the OS. */
class FakeNativeTheme implements NativeThemeLike {
  themeSourceValue: "system" | "light" | "dark" = "system"
  osDark = true
  private readonly listeners = new Set<() => void>()
  get themeSource() { return this.themeSourceValue }
  set themeSource(value) { this.themeSourceValue = value }
  get shouldUseDarkColors() { return this.themeSourceValue === "system" ? this.osDark : this.themeSourceValue === "dark" }
  on(_event: "updated", listener: () => void) { this.listeners.add(listener) }
  removeListener(_event: "updated", listener: () => void) { this.listeners.delete(listener) }
  flipOs(dark: boolean) { this.osDark = dark; for (const listener of this.listeners) listener() }
  get listenerCount() { return this.listeners.size }
}

let directory: string
let filePath: string

beforeEach(() => {
  directory = mkdtempSync(path.join(os.tmpdir(), "agora-appearance-"))
  filePath = appearanceFilePath(directory)
})
function seed(contents: string) {
  mkdirSync(path.dirname(filePath), { recursive: true })
  writeFileSync(filePath, contents)
}
afterEach(() => rmSync(directory, { recursive: true, force: true }))

describe("desktop appearance", () => {
  it("uses the Rakazo workspace background for each resolved theme", () => {
    expect(windowBackgroundFor("light")).toBe("#ffffff")
    expect(windowBackgroundFor("dark")).toBe("#161616")
  })

  it("accepts only the three appearance preferences the workspace offers", () => {
    expect(["system", "light", "dark"].map(validateAppearancePreference)).toEqual(["system", "light", "dark"])
    for (const value of ["Dark", "", null, 1, { preference: "dark" }, "high-contrast"]) {
      expect(() => validateAppearancePreference(value)).toThrow("appearance_preference_invalid")
    }
  })

  it("keeps the preference inside the single host-owned userData directory", () => {
    expect(path.relative(directory, filePath).split(path.sep)).toEqual(["host-preferences", "appearance.json"])
  })

  it("falls back to following the system for a missing, corrupt or foreign preference file", () => {
    expect(readAppearancePreference(filePath)).toBe("system")
    seed("{not json")
    expect(readAppearancePreference(filePath)).toBe("system")
    seed(JSON.stringify({ schema: "other/v1", preference: "dark" }))
    expect(readAppearancePreference(filePath)).toBe("system")
    seed(JSON.stringify({ schema: "agora.desktop-appearance/v1", preference: "sepia" }))
    expect(readAppearancePreference(filePath)).toBe("system")
  })

  it("applies the saved preference at launch and persists every change", async () => {
    seed(JSON.stringify({ schema: "agora.desktop-appearance/v1", preference: "light" }))
    const nativeTheme = new FakeNativeTheme()
    const appearance = new DesktopAppearance(nativeTheme, filePath)
    expect(nativeTheme.themeSource).toBe("light")
    expect(appearance.view).toEqual({ preference: "light", theme: "light" })

    await expect(appearance.set("dark")).resolves.toEqual({ preference: "dark", theme: "dark" })
    expect(nativeTheme.themeSource).toBe("dark")
    expect(JSON.parse(readFileSync(filePath, "utf8"))).toEqual({ schema: "agora.desktop-appearance/v1", preference: "dark" })
    expect(new DesktopAppearance(new FakeNativeTheme(), filePath).view.preference).toBe("dark")
  })

  it("keeps the last change on disk when changes arrive back to back", async () => {
    const appearance = new DesktopAppearance(new FakeNativeTheme(), filePath)
    await Promise.all([appearance.set("light"), appearance.set("dark"), appearance.set("system")])
    expect(readAppearancePreference(filePath)).toBe("system")
  })

  it("rejects an invalid preference without touching the applied theme or the file", async () => {
    const nativeTheme = new FakeNativeTheme()
    const appearance = new DesktopAppearance(nativeTheme, filePath)
    await expect(appearance.set("sepia")).rejects.toThrow("appearance_preference_invalid")
    expect(nativeTheme.themeSource).toBe("system")
    expect(readAppearancePreference(filePath)).toBe("system")
  })

  it("notifies when the resolved theme changes, including OS flips while following the system", async () => {
    const nativeTheme = new FakeNativeTheme()
    const appearance = new DesktopAppearance(nativeTheme, filePath)
    const listener = vi.fn()
    appearance.onChange(listener)

    nativeTheme.flipOs(false)
    expect(listener).toHaveBeenLastCalledWith({ preference: "system", theme: "light" })
    await appearance.set("light")
    expect(listener).toHaveBeenCalledTimes(1)
    await appearance.set("dark")
    expect(listener).toHaveBeenLastCalledWith({ preference: "dark", theme: "dark" })
    nativeTheme.flipOs(true)
    expect(listener).toHaveBeenCalledTimes(2)

    appearance.dispose()
    expect(nativeTheme.listenerCount).toBe(0)
  })
})
