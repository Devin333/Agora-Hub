import type { IpcMainInvokeEvent } from "electron"
import { IPC_CHANNELS } from "./channels"

const MAINTENANCE_CHANNELS = new Set<string>([
  IPC_CHANNELS.runtimeClearDataPreview,
  IPC_CHANNELS.runtimeClearDataApply,
  IPC_CHANNELS.runtimeClearDataStatus,
  IPC_CHANNELS.lifecycleUpdate,
  IPC_CHANNELS.lifecycleRespond
])

interface DocumentIdentity {
  webContentsId: number
  processId: number
  routingId: number
}

export interface WindowRequestDetails {
  webContentsId?: number
  url: string
}

function identity(event: IpcMainInvokeEvent): DocumentIdentity | null {
  const frame = event.senderFrame
  if (!frame || frame.isDestroyed()) return null
  return { webContentsId: event.sender.id, processId: frame.processId, routingId: frame.routingId }
}

function matches(expected: DocumentIdentity | null, actual: DocumentIdentity): boolean {
  return expected !== null &&
    actual.webContentsId === expected.webContentsId &&
    actual.processId === expected.processId &&
    actual.routingId === expected.routingId
}

export class OwnedWindowMaintenanceFence {
  private document: DocumentIdentity | null = null
  private authorizedDocument: DocumentIdentity | null = null

  get active(): boolean { return this.document !== null }

  authorizeDocument(webContentsId: number, processId: number, routingId: number): void {
    this.authorizedDocument = { webContentsId, processId, routingId }
  }

  revokeDocument(webContentsId: number): void {
    if (this.authorizedDocument?.webContentsId === webContentsId) this.authorizedDocument = null
  }

  isAuthorizedDocument(event: IpcMainInvokeEvent): boolean {
    const current = identity(event)
    return current !== null && matches(this.authorizedDocument, current)
  }

  enter(event: IpcMainInvokeEvent): boolean {
    const current = identity(event)
    if (!current) return false
    if (!this.document) {
      this.document = current
      return true
    }
    return matches(this.document, current)
  }

  allowsIpc(event: IpcMainInvokeEvent, channel: string): boolean {
    if (!this.document) return true
    const current = identity(event)
    return current !== null && MAINTENANCE_CHANNELS.has(channel) && matches(this.document, current)
  }

  blocksNavigation(webContentsId: number): boolean {
    return this.document?.webContentsId === webContentsId
  }

  blocksInputReload(webContentsId: number, key: string, control: boolean, meta: boolean, shift: boolean): boolean {
    if (!this.blocksNavigation(webContentsId)) return false
    const normalized = key.toLowerCase()
    return normalized === "f5" || ((control || meta) && normalized === "r") || ((control || meta) && shift && normalized === "r")
  }

  blocksRequest(details: WindowRequestDetails): boolean {
    if (!this.document || details.webContentsId !== this.document.webContentsId) return false
    try {
      const protocol = new URL(details.url).protocol
      return protocol === "http:" || protocol === "https:" || protocol === "ws:" || protocol === "wss:"
    } catch {
      return true
    }
  }
}
