import { create } from "zustand"
import { createJSONStorage, persist } from "zustand/middleware"
import {
  DEFAULT_APPEARANCE,
  UI_STORE_STORAGE_KEY,
  isAppearancePreference,
  type AppearancePreference,
  type ResolvedTheme,
} from "@/lib/theme/appearance"

export type ThemeMode = ResolvedTheme
export type LocaleMode = "zh" | "en"

type UiState = {
  sidebarCollapsed: boolean
  commandOpen: boolean
  rightPanelOpen: boolean
  /** What the user picked; "system" follows the platform color scheme. */
  appearance: AppearancePreference
  /** The palette currently applied; kept current by ThemeBridge. */
  theme: ThemeMode
  locale: LocaleMode
  setSidebarCollapsed: (value: boolean) => void
  toggleSidebar: () => void
  setCommandOpen: (value: boolean) => void
  setRightPanelOpen: (value: boolean) => void
  setAppearance: (appearance: AppearancePreference) => void
  /** Pins an explicit palette, leaving "system" mode. */
  setTheme: (theme: ThemeMode) => void
  toggleTheme: () => void
  setResolvedTheme: (theme: ThemeMode) => void
  setLocale: (locale: LocaleMode) => void
  toggleLocale: () => void
}

export const useUiStore = create<UiState>()(
  persist(
    (set) => ({
      sidebarCollapsed: false,
      commandOpen: false,
      rightPanelOpen: true,
      appearance: DEFAULT_APPEARANCE,
      theme: "dark",
      locale: "zh",
      setSidebarCollapsed: (value) => set({ sidebarCollapsed: value }),
      toggleSidebar: () => set((state) => ({ sidebarCollapsed: !state.sidebarCollapsed })),
      setCommandOpen: (value) => set({ commandOpen: value }),
      setRightPanelOpen: (value) => set({ rightPanelOpen: value }),
      setAppearance: (appearance) => set({ appearance }),
      setTheme: (theme) => set({ appearance: theme, theme }),
      toggleTheme: () => set((state) => {
        const theme = state.theme === "dark" ? "light" : "dark"
        return { appearance: theme, theme }
      }),
      setResolvedTheme: (theme) => set({ theme }),
      setLocale: (locale) => set({ locale }),
      toggleLocale: () => set((state) => ({ locale: state.locale === "zh" ? "en" : "zh" }))
    }),
    {
      name: UI_STORE_STORAGE_KEY,
      version: 1,
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        sidebarCollapsed: state.sidebarCollapsed,
        rightPanelOpen: state.rightPanelOpen,
        appearance: state.appearance,
        locale: state.locale
      }),
      // v0 persisted only the palette and defaulted to "light", so a stored "light"
      // cannot be told apart from never choosing; only an explicit "dark" stays pinned.
      migrate: (persisted, version) => {
        const state = (persisted ?? {}) as Record<string, unknown>
        if (version < 1) {
          const { theme, ...rest } = state
          return { ...rest, appearance: theme === "dark" ? "dark" : DEFAULT_APPEARANCE }
        }
        return { ...state, appearance: isAppearancePreference(state.appearance) ? state.appearance : DEFAULT_APPEARANCE }
      }
    }
  )
)
