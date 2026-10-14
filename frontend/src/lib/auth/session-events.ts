export const portalSessionChangedEvent = "newsroom:portal-session-changed"

export function notifyPortalSessionChanged(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(portalSessionChangedEvent))
  }
}
