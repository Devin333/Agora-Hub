/**
 * UI appearance: the user's stored preference and the theme it resolves to.
 *
 * `public/theme-init.js` repeats `resolveTheme` and `applyResolvedTheme` in plain
 * JavaScript so the first paint already uses the right palette; keep the two in
 * step (`appearance.test.ts` checks the shared constants).
 */
export const APPEARANCE_PREFERENCES = ["system", "light", "dark"] as const
export type AppearancePreference = (typeof APPEARANCE_PREFERENCES)[number]
export type ResolvedTheme = "light" | "dark"

/** zustand `persist` key of the UI store that owns the preference. */
export const UI_STORE_STORAGE_KEY = "newsroom-ui"
export const DEFAULT_APPEARANCE: AppearancePreference = "system"
export const THEME_COLORS: Record<ResolvedTheme, string> = { light: "#ffffff", dark: "#161616" }

const LIGHT_SCHEME_QUERY = "(prefers-color-scheme: light)"

export function isAppearancePreference(value: unknown): value is AppearancePreference {
  return typeof value === "string" && (APPEARANCE_PREFERENCES as readonly string[]).includes(value)
}

/** Dark is the fallback whenever the platform does not ask for light. */
export function resolveTheme(preference: AppearancePreference, systemPrefersLight: boolean): ResolvedTheme {
  if (preference === "system") return systemPrefersLight ? "light" : "dark"
  return preference
}

export function systemPrefersLight(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function" && window.matchMedia(LIGHT_SCHEME_QUERY).matches
}

export function applyResolvedTheme(theme: ResolvedTheme, root: HTMLElement = document.documentElement): void {
  root.dataset.theme = theme
  root.style.colorScheme = theme
  const meta = root.ownerDocument.querySelector('meta[name="theme-color"]')
  if (meta) meta.setAttribute("content", THEME_COLORS[theme])
}

/** Calls `onChange` whenever the platform color scheme flips; returns the unsubscribe. */
export function watchSystemAppearance(onChange: (prefersLight: boolean) => void): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => undefined
  const query = window.matchMedia(LIGHT_SCHEME_QUERY)
  const listener = (event: MediaQueryListEvent) => onChange(event.matches)
  query.addEventListener("change", listener)
  return () => query.removeEventListener("change", listener)
}
