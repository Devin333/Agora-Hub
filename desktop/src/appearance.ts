import { randomUUID } from "node:crypto"
import { readFileSync } from "node:fs"
import { mkdir, rename, rm, writeFile } from "node:fs/promises"
import path from "node:path"
import type { AppearancePreference, AppearanceView, ResolvedTheme } from "./contracts"

/**
 * Host side of the appearance preference the workspace offers (跟随系统 / 浅色 / 深色).
 *
 * The renderer owns the choice and reports it here; the host applies it to
 * `nativeTheme.themeSource`, so the startup page's `prefers-color-scheme`, native
 * dialogs and the window background follow the same palette, and persists it so
 * the next launch paints the right color before the workspace has loaded.
 */
export const APPEARANCE_PREFERENCES: readonly AppearancePreference[] = ["system", "light", "dark"]
export const DEFAULT_APPEARANCE: AppearancePreference = "system"
/**
 * Host-owned preferences live in one top-level userData directory so temporary
 * write files never appear as unknown profile owners; scripts/desktop_app/owned_clear.py
 * lists this name among the host entries a full data clear removes.
 */
export const HOST_PREFERENCES_DIRECTORY = "host-preferences"
export const APPEARANCE_FILE_NAME = "appearance.json"

export function appearanceFilePath(userDataPath: string): string {
  return path.join(userDataPath, HOST_PREFERENCES_DIRECTORY, APPEARANCE_FILE_NAME)
}
const APPEARANCE_SCHEMA = "agora.desktop-appearance/v1"

/** Rakazo `--background` per theme; mirrors frontend/src/styles/theme-tokens.css. */
export const WINDOW_BACKGROUNDS: Readonly<Record<ResolvedTheme, string>> = Object.freeze({ light: "#ffffff", dark: "#161616" })

export function windowBackgroundFor(theme: ResolvedTheme): string {
  return WINDOW_BACKGROUNDS[theme]
}

export function validateAppearancePreference(value: unknown): AppearancePreference {
  if (typeof value === "string" && (APPEARANCE_PREFERENCES as readonly string[]).includes(value)) return value as AppearancePreference
  throw new Error("appearance_preference_invalid")
}

/** Reads the persisted preference; a missing, unreadable or foreign file means "follow the system". */
export function readAppearancePreference(filePath: string): AppearancePreference {
  try {
    const parsed: unknown = JSON.parse(readFileSync(filePath, "utf8"))
    if (!parsed || typeof parsed !== "object") return DEFAULT_APPEARANCE
    const record = parsed as Record<string, unknown>
    if (record.schema !== APPEARANCE_SCHEMA) return DEFAULT_APPEARANCE
    return validateAppearancePreference(record.preference)
  } catch {
    return DEFAULT_APPEARANCE
  }
}

/** Replaces the preference file atomically so a crash never leaves a torn document. */
export async function writeAppearancePreference(filePath: string, preference: AppearancePreference): Promise<void> {
  await mkdir(path.dirname(filePath), { recursive: true })
  const temporary = `${filePath}.${randomUUID()}.tmp`
  try {
    await writeFile(temporary, `${JSON.stringify({ schema: APPEARANCE_SCHEMA, preference })}\n`, { encoding: "utf8", flag: "wx" })
    await rename(temporary, filePath)
  } catch (error) {
    await rm(temporary, { force: true })
    throw error
  }
}

export interface NativeThemeLike {
  themeSource: AppearancePreference
  readonly shouldUseDarkColors: boolean
  on(event: "updated", listener: () => void): unknown
  removeListener(event: "updated", listener: () => void): unknown
}

export interface DesktopAppearanceController {
  readonly view: AppearanceView
  set(value: unknown): Promise<AppearanceView>
  onChange(listener: (view: AppearanceView) => void): () => void
}

export class DesktopAppearance implements DesktopAppearanceController {
  private readonly listeners = new Set<(view: AppearanceView) => void>()
  private persisted: Promise<void> = Promise.resolve()
  private lastTheme: ResolvedTheme

  constructor(private readonly nativeTheme: NativeThemeLike, private readonly filePath: string) {
    this.nativeTheme.themeSource = readAppearancePreference(filePath)
    this.lastTheme = this.resolvedTheme
    // Fires for an explicit change and for an OS scheme flip while following the system.
    this.nativeTheme.on("updated", this.handleNativeUpdate)
  }

  get view(): AppearanceView {
    return { preference: this.nativeTheme.themeSource, theme: this.resolvedTheme }
  }

  async set(value: unknown): Promise<AppearanceView> {
    const preference = validateAppearancePreference(value)
    if (preference !== this.nativeTheme.themeSource) {
      this.nativeTheme.themeSource = preference
      this.handleNativeUpdate()
    }
    // Writes are chained so rapid toggles land on disk in the order they were made.
    const write = this.persisted.then(() => writeAppearancePreference(this.filePath, preference))
    this.persisted = write.catch(() => undefined)
    await write
    return this.view
  }

  onChange(listener: (view: AppearanceView) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  dispose(): void {
    this.nativeTheme.removeListener("updated", this.handleNativeUpdate)
    this.listeners.clear()
  }

  private get resolvedTheme(): ResolvedTheme {
    return this.nativeTheme.shouldUseDarkColors ? "dark" : "light"
  }

  private readonly handleNativeUpdate = (): void => {
    const theme = this.resolvedTheme
    if (theme === this.lastTheme) return
    this.lastTheme = theme
    const view = this.view
    for (const listener of this.listeners) listener(view)
  }
}
