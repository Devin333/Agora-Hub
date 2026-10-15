import type { AgoraDesktopApi } from "./contracts"

declare global {
  interface Window {
    agoraDesktop?: AgoraDesktopApi
  }
}

export {}
