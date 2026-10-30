// Lets any research surface send the user to the settings section that fixes
// the failure it hit, without importing the settings dialog itself.

export const SETTINGS_SECTIONS = ["account", "model", "capabilities", "network", "admin"] as const
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number]

export const openSettingsEvent = "agora:open-settings"

export type OpenSettingsDetail = { section: SettingsSection }

// The workbench that owns the settings dialog claims the event with preventDefault();
// on any other page nothing claims it, so the user is sent to the workbench instead.
export const settingsPagePath = "/paper-agent"
export const settingsQueryKey = "settings"

export function openSettings(section: SettingsSection, navigate: (href: string) => void = href => window.location.assign(href)): void {
  if (typeof window === "undefined") return
  const event = new CustomEvent<OpenSettingsDetail>(openSettingsEvent, { detail: { section }, cancelable: true })
  if (window.dispatchEvent(event)) navigate(settingsHref(section))
}

export function settingsHref(section: SettingsSection): string {
  return `${settingsPagePath}?${new URLSearchParams({ [settingsQueryKey]: section })}`
}

export function parseSettingsSection(value: string | null | undefined): SettingsSection | null {
  return value && (SETTINGS_SECTIONS as readonly string[]).includes(value) ? value as SettingsSection : null
}

export function settingsSectionFrom(event: Event): SettingsSection {
  return parseSettingsSection((event as CustomEvent<Partial<OpenSettingsDetail> | null>).detail?.section) ?? "account"
}

// Failures the user resolves in their model connection settings: assigning a
// connection to a purpose, entering a key again, or choosing a model whose
// window holds the purpose (interfaces/services/model_connections/errors.py).
const MODEL_CONNECTION_CODES = new Set([
  "model_connection_required",
  "model_connection_key_missing",
  "model_connection_key_rotated",
  "model_connection_rejected",
  "model_connection_withdrawn",
  "model_connection_window_too_small",
  "model_connection_structured_output_required",
])

export function isModelConnectionFailure(code: string | null | undefined): boolean {
  return typeof code === "string" && MODEL_CONNECTION_CODES.has(code)
}
