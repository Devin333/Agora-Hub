import type { AgoraConnectionApi, AgoraDesktopApi } from "./contracts"

declare global {
  interface Window {
    agoraDesktop?: AgoraDesktopApi
    agoraConnection?: AgoraConnectionApi
  }
}

export {}
