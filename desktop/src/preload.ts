import { contextBridge, ipcRenderer } from "electron"
import type { AgoraDesktopApi, DataClearApplyRequest, DesktopConfigApplyRequest, DesktopConfigCandidate, ExportRequest, QuitRequest, QuitResponse, RendererLifecycleState, RestoreActivateRequest } from "./contracts"
import { IPC_CHANNELS } from "./channels"

const api: AgoraDesktopApi = Object.freeze({
  version: 1 as const,
  runtime: Object.freeze({
    status: () => ipcRenderer.invoke(IPC_CHANNELS.runtimeStatus),
    retry: () => ipcRenderer.invoke(IPC_CHANNELS.runtimeRetry),
    diagnostics: () => ipcRenderer.invoke(IPC_CHANNELS.runtimeDiagnostics),
    backup: () => ipcRenderer.invoke(IPC_CHANNELS.runtimeBackup),
    restore: Object.freeze({
      preview: () => ipcRenderer.invoke(IPC_CHANNELS.runtimeRestorePreview),
      activate: (request: RestoreActivateRequest) => ipcRenderer.invoke(IPC_CHANNELS.runtimeRestoreActivate, request)
    }),
    clearData: Object.freeze({
      preview: () => ipcRenderer.invoke(IPC_CHANNELS.runtimeClearDataPreview),
      apply: (request: DataClearApplyRequest) => ipcRenderer.invoke(IPC_CHANNELS.runtimeClearDataApply, request),
      status: () => ipcRenderer.invoke(IPC_CHANNELS.runtimeClearDataStatus)
    })
  }),
  config: Object.freeze({
    show: () => ipcRenderer.invoke(IPC_CHANNELS.configShow),
    preview: (candidate: DesktopConfigCandidate) => ipcRenderer.invoke(IPC_CHANNELS.configPreview, candidate),
    apply: (request: DesktopConfigApplyRequest) => ipcRenderer.invoke(IPC_CHANNELS.configApply, request)
  }),
  credentials: Object.freeze({
    status: () => ipcRenderer.invoke(IPC_CHANNELS.credentialStatus),
    replace: () => ipcRenderer.invoke(IPC_CHANNELS.credentialReplace),
    remove: () => ipcRenderer.invoke(IPC_CHANNELS.credentialRemove)
  }),
  lifecycle: Object.freeze({
    updateState: (state: RendererLifecycleState) => ipcRenderer.invoke(IPC_CHANNELS.lifecycleUpdate, state),
    onQuitRequested: (listener: (request: QuitRequest) => void) => {
      const wrapped = (_event: Electron.IpcRendererEvent, request: QuitRequest) => listener(request)
      ipcRenderer.on(IPC_CHANNELS.lifecycleQuitRequested, wrapped)
      return () => ipcRenderer.removeListener(IPC_CHANNELS.lifecycleQuitRequested, wrapped)
    },
    respondToQuit: (response: QuitResponse) => ipcRenderer.invoke(IPC_CHANNELS.lifecycleRespond, response)
  }),
  export: Object.freeze({ save: (request: ExportRequest) => ipcRenderer.invoke(IPC_CHANNELS.exportSave, request) })
})

contextBridge.exposeInMainWorld("agoraDesktop", api)


