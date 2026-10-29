import path from "node:path"
import { dialog, ipcMain, type IpcMainInvokeEvent } from "electron"
import type { DesktopAppearanceController } from "./appearance"
import type { AuditLogger } from "./audit-log"
import { APP_CHANNELS, CONNECTION_CHANNELS } from "./channels"
import { exportFailure, validateExportRequest, writeExportFile } from "./export-service"
import type { RendererLifecycleBridge } from "./lifecycle-bridge"
import { isConnectionPage, isWorkspaceUrl } from "./navigation-policy"
import type { WorkspaceConnection } from "./workspace-connection"

/**
 * The host's invoke handlers. Each channel answers exactly one kind of
 * document: the workspace channels only the main frame of a workspace page on
 * the server the window is connected to, the connection channels only the
 * packaged connection page. Anything else is refused before the handler runs.
 */
export interface RegisterIpcOptions {
  connection: WorkspaceConnection
  lifecycle: RendererLifecycleBridge
  appearance: DesktopAppearanceController
  connectionFile: string
  documentsPath: string
  logger: Pick<AuditLogger, "write">
}

export type IpcSourceKind = "workspace" | "connection"

interface IpcSourceEvent {
  senderFrame: { url: string; parent: unknown } | null
}

/** Whether the frame that sent `event` may use channels of `kind`. */
export function isTrustedIpcSource(event: IpcSourceEvent, kind: IpcSourceKind, workspaceOrigin: string | null, connectionFile: string): boolean {
  const frame = event.senderFrame
  // Subframes never get a bridge; a destroyed frame cannot prove where it came from.
  if (!frame || frame.parent) return false
  if (kind === "connection") return isConnectionPage(frame.url, connectionFile)
  return workspaceOrigin !== null && isWorkspaceUrl(frame.url, workspaceOrigin)
}

export function registerIpcHandlers(options: RegisterIpcOptions): () => void {
  const handle = (channel: string, kind: IpcSourceKind, listener: (value: unknown) => unknown) => {
    ipcMain.handle(channel, async (event: IpcMainInvokeEvent, value?: unknown) => {
      if (!isTrustedIpcSource(event, kind, options.connection.workspaceOrigin, options.connectionFile)) {
        await options.logger.write("ipc_source_rejected", { channel })
        throw new Error("ipc_source_rejected")
      }
      return await listener(value)
    })
  }

  handle(APP_CHANNELS.lifecycleUpdate, "workspace", state => options.lifecycle.update(state))
  handle(APP_CHANNELS.lifecycleRespond, "workspace", response => options.lifecycle.respond(response))
  handle(APP_CHANNELS.exportSave, "workspace", async value => {
    try {
      const request = validateExportRequest(value)
      const result = await dialog.showSaveDialog({
        title: "导出 Agora 内容",
        defaultPath: path.join(options.documentsPath, request.fileName),
        buttonLabel: "导出",
        properties: ["showOverwriteConfirmation", "createDirectory"],
        filters: [{ name: request.format.toUpperCase(), extensions: [request.format] }]
      })
      if (result.canceled || !result.filePath) return { ok: false, code: "cancelled", message: "已取消导出" }
      await writeExportFile(result.filePath, request)
      await options.logger.write("export_saved", { format: request.format, bytes: request.byteLength, fileName: path.basename(result.filePath) })
      return { ok: true, code: "saved", fileName: path.basename(result.filePath), message: "导出完成" }
    } catch (error) {
      await options.logger.write("export_failed", { error: error instanceof Error ? error.message : "unknown" })
      return exportFailure(error)
    }
  })
  handle(APP_CHANNELS.appearanceSet, "workspace", value => options.appearance.set(value))

  handle(CONNECTION_CHANNELS.status, "connection", () => options.connection.status)
  handle(CONNECTION_CHANNELS.retry, "connection", () => options.connection.retry())
  handle(CONNECTION_CHANNELS.save, "connection", address => options.connection.save(address))
  handle(CONNECTION_CHANNELS.reset, "connection", () => options.connection.reset())
  // The sender is the connection page, so no workspace with unsaved work is being left.
  handle(CONNECTION_CHANNELS.edit, "connection", async () => {
    await options.connection.edit()
    return options.connection.status
  })
  handle(CONNECTION_CHANNELS.cancelEdit, "connection", () => options.connection.cancelEdit())

  return () => {
    for (const channel of [...Object.values(APP_CHANNELS), ...Object.values(CONNECTION_CHANNELS)]) ipcMain.removeHandler(channel)
  }
}
