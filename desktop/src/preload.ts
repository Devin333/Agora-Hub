import { contextBridge, ipcRenderer } from "electron"
import type { AgoraConnectionApi, AgoraDesktopApi, AppearancePreference, ConnectionChoice, ConnectionStatusView, LocalAdminRequest, ExportRequest, QuitRequest, QuitResponse, RendererLifecycleState } from "./contracts"
import { APP_CHANNELS, CONNECTION_CHANNELS, HOST_EVENTS } from "./channels"

/**
 * Each document gets only the bridge meant for it: the packaged connection page
 * (`file:`) the connection controls, a page of the server the workspace
 * bridge. The main process checks every call's sender again, so this split
 * only keeps each page's surface minimal; it is not the security boundary.
 */
function subscribe<T>(channel: string, listener: (value: T) => void): () => void {
  const wrapped = (_event: Electron.IpcRendererEvent, value: T) => listener(value)
  ipcRenderer.on(channel, wrapped)
  return () => ipcRenderer.removeListener(channel, wrapped)
}

if (location.protocol === "file:") {
  const connection: AgoraConnectionApi = Object.freeze({
    version: 2 as const,
    status: () => ipcRenderer.invoke(CONNECTION_CHANNELS.status),
    retry: () => ipcRenderer.invoke(CONNECTION_CHANNELS.retry),
    choose: (choice: ConnectionChoice) => ipcRenderer.invoke(CONNECTION_CHANNELS.choose, choice),
    edit: () => ipcRenderer.invoke(CONNECTION_CHANNELS.edit),
    cancelEdit: () => ipcRenderer.invoke(CONNECTION_CHANNELS.cancelEdit),
    onChange: (listener: (status: ConnectionStatusView) => void) => subscribe(HOST_EVENTS.connectionChanged, listener),
    local: Object.freeze({
      createAdmin: (request: LocalAdminRequest) => ipcRenderer.invoke(CONNECTION_CHANNELS.localCreateAdmin, request),
      cancel: () => ipcRenderer.invoke(CONNECTION_CHANNELS.localCancel)
    })
  })
  contextBridge.exposeInMainWorld("agoraConnection", connection)
} else if (location.protocol === "https:" || location.protocol === "http:") {
  const desktop: AgoraDesktopApi = Object.freeze({
    version: 1 as const,
    lifecycle: Object.freeze({
      updateState: (state: RendererLifecycleState) => ipcRenderer.invoke(APP_CHANNELS.lifecycleUpdate, state),
      onQuitRequested: (listener: (request: QuitRequest) => void) => subscribe(HOST_EVENTS.lifecycleQuitRequested, listener),
      respondToQuit: (response: QuitResponse) => ipcRenderer.invoke(APP_CHANNELS.lifecycleRespond, response)
    }),
    export: Object.freeze({ save: (request: ExportRequest) => ipcRenderer.invoke(APP_CHANNELS.exportSave, request) }),
    appearance: Object.freeze({ set: (preference: AppearancePreference) => ipcRenderer.invoke(APP_CHANNELS.appearanceSet, preference) })
  })
  contextBridge.exposeInMainWorld("agoraDesktop", desktop)
}
