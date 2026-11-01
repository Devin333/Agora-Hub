// Model-backed capabilities depend on the account's own model connections, so
// every surface that shows them re-reads when a connection or an assignment
// is saved, verified or deleted.
export const modelConnectionsChangedEvent = "agora:model-connections-changed"

export function notifyModelConnectionsChanged(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(modelConnectionsChangedEvent))
  }
}
