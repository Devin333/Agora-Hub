// Model-backed capabilities depend on the account's own API key, so every
// surface that shows them re-reads when the key is saved, verified or deleted.
export const modelCredentialChangedEvent = "agora:model-credential-changed"

export function notifyModelCredentialChanged(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(modelCredentialChangedEvent))
  }
}
