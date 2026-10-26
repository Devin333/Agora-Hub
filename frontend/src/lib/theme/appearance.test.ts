import { readFileSync } from "node:fs"
import { join } from "node:path"
import { afterEach, describe, expect, it, vi } from "vitest"
import {
  DEFAULT_APPEARANCE,
  THEME_COLORS,
  UI_STORE_STORAGE_KEY,
  applyResolvedTheme,
  isAppearancePreference,
  resolveTheme,
  watchSystemAppearance,
  type AppearancePreference
} from "./appearance"

const initScript = readFileSync(join(process.cwd(), "public/theme-init.js"), "utf-8")

function stubColorScheme(prefersLight: boolean) {
  const listeners = new Set<(event: MediaQueryListEvent) => void>()
  const query = {
    matches: prefersLight,
    addEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => listeners.delete(listener)
  }
  vi.stubGlobal("matchMedia", vi.fn(() => query))
  return {
    listeners,
    flip(next: boolean) {
      query.matches = next
      listeners.forEach((listener) => listener({ matches: next } as MediaQueryListEvent))
    }
  }
}

function runInitScript() {
  new Function(initScript)()
}

function resetRoot() {
  const root = document.documentElement
  delete root.dataset.theme
  root.style.colorScheme = ""
  document.head.innerHTML = '<meta name="theme-color" content="">'
}

afterEach(() => {
  vi.unstubAllGlobals()
  localStorage.clear()
  resetRoot()
})

describe("resolveTheme", () => {
  it("follows the platform in system mode and falls back to dark", () => {
    expect(resolveTheme("system", true)).toBe("light")
    expect(resolveTheme("system", false)).toBe("dark")
  })

  it("keeps an explicit pick regardless of the platform", () => {
    expect(resolveTheme("light", false)).toBe("light")
    expect(resolveTheme("dark", true)).toBe("dark")
  })

  it("only accepts known preferences", () => {
    expect(isAppearancePreference("system")).toBe(true)
    expect(isAppearancePreference("sepia")).toBe(false)
    expect(isAppearancePreference(undefined)).toBe(false)
  })
})

describe("applyResolvedTheme", () => {
  it("sets data-theme, color-scheme and the browser chrome color", () => {
    resetRoot()
    applyResolvedTheme("light")
    const root = document.documentElement
    expect(root.dataset.theme).toBe("light")
    expect(root.style.colorScheme).toBe("light")
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute("content")).toBe(THEME_COLORS.light)
  })
})

describe("watchSystemAppearance", () => {
  it("reports platform flips until unsubscribed", () => {
    const scheme = stubColorScheme(false)
    const onChange = vi.fn()
    const stop = watchSystemAppearance(onChange)
    scheme.flip(true)
    expect(onChange).toHaveBeenCalledWith(true)
    stop()
    expect(scheme.listeners.size).toBe(0)
  })
})

describe("public/theme-init.js", () => {
  it("shares the storage key, default preference and chrome colors", () => {
    expect(initScript).toContain(`const storageKey = "${UI_STORE_STORAGE_KEY}"`)
    expect(initScript).toContain(`let preference = "${DEFAULT_APPEARANCE}"`)
    expect(initScript).toContain(`const themeColors = { light: "${THEME_COLORS.light}", dark: "${THEME_COLORS.dark}" }`)
  })

  const cases: Array<{ stored: AppearancePreference | null; prefersLight: boolean }> = [
    { stored: null, prefersLight: true },
    { stored: null, prefersLight: false },
    { stored: "system", prefersLight: true },
    { stored: "light", prefersLight: false },
    { stored: "dark", prefersLight: true }
  ]

  it.each(cases)("resolves like resolveTheme (stored=$stored, prefersLight=$prefersLight)", ({ stored, prefersLight }) => {
    resetRoot()
    stubColorScheme(prefersLight)
    if (stored) localStorage.setItem(UI_STORE_STORAGE_KEY, JSON.stringify({ state: { appearance: stored }, version: 1 }))

    runInitScript()

    const expected = resolveTheme(stored ?? DEFAULT_APPEARANCE, prefersLight)
    expect(document.documentElement.dataset.theme).toBe(expected)
    expect(document.documentElement.style.colorScheme).toBe(expected)
    expect(document.querySelector('meta[name="theme-color"]')?.getAttribute("content")).toBe(THEME_COLORS[expected])
  })

  it("ignores corrupt or unknown stored values", () => {
    resetRoot()
    stubColorScheme(true)
    localStorage.setItem(UI_STORE_STORAGE_KEY, "{not json")
    runInitScript()
    expect(document.documentElement.dataset.theme).toBe("light")

    resetRoot()
    localStorage.setItem(UI_STORE_STORAGE_KEY, JSON.stringify({ state: { appearance: "sepia" } }))
    runInitScript()
    expect(document.documentElement.dataset.theme).toBe("light")
  })
})
