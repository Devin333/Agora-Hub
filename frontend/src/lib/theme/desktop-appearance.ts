import type { AppearancePreference } from "@/lib/theme/appearance"

/** The slice of the desktop host bridge (desktop/src/preload.ts) that owns the native appearance. */
type DesktopAppearanceBridge = { set(preference: AppearancePreference): Promise<unknown> }
type DesktopAppearanceWindow = Window & { agoraDesktop?: { version: 1; appearance?: DesktopAppearanceBridge } }

/**
 * Reports the user's appearance choice to the desktop host so the window
 * background, native dialogs and the startup page follow it on this and the next
 * launch. In a plain browser there is no bridge and this does nothing; the host
 * rejects untrusted origins, and a failed report only leaves the native chrome on
 * the previous palette, so errors are not surfaced to the page.
 */
export function syncDesktopAppearance(preference: AppearancePreference): void {
  if (typeof window === "undefined") return
  const bridge = (window as DesktopAppearanceWindow).agoraDesktop?.appearance
  if (!bridge) return
  void bridge.set(preference).catch(() => undefined)
}
