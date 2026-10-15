import path from "node:path"
import { BrowserWindow, dialog, ipcMain, type IpcMainInvokeEvent, type MessageBoxOptions, type OpenDialogOptions, type SaveDialogOptions } from "electron"
import type { DataClearPreviewResult, DesktopConfigPreviewResult, RuntimeStatusView } from "./contracts"
import type { RuntimeController } from "./runtime-coordinator"
import type { RendererLifecycleBridge } from "./lifecycle-bridge"
import type { RestoreNativeConfirmation, RuntimeMaintenanceController } from "./runtime-maintenance"
import { assertSafeBackupSource, assertSafeBackupTarget, NativePathValidationError } from "./native-backup-path"
import { exportFailure, validateExportRequest, writeExportFile } from "./export-service"
import { isAllowedApplicationUrl, isTrustedIpcSource } from "./navigation-policy"
import type { AuditLogger } from "./audit-log"
import { IPC_CHANNELS } from "./channels"
import type { DesktopConfigController } from "./desktop-config-bridge"
import type { CredentialController } from "./native-credential-bridge"
import { requiresNativeQuitConfirmation } from "./lifecycle-bridge"
import type { OwnedDataClearController } from "./owned-data-clear"
import type { OwnedWindowMaintenanceFence } from "./owned-window-maintenance"

export { IPC_CHANNELS } from "./channels"

const CLEAR_DATA_CHANNELS = new Set<string>([
  IPC_CHANNELS.runtimeClearDataPreview,
  IPC_CHANNELS.runtimeClearDataApply,
  IPC_CHANNELS.runtimeClearDataStatus
])

interface RegisterIpcOptions {
  coordinator: RuntimeController
  lifecycle: RendererLifecycleBridge
  maintenance: RuntimeMaintenanceController
  config: DesktopConfigController
  credentials: CredentialController
  clearData: OwnedDataClearController
  maintenanceFence: OwnedWindowMaintenanceFence
  startupFile: string
  getCanonicalOrigin(): string | null
  documentsPath: string
  logger: Pick<AuditLogger, "write">
  onRuntimeReady(status: RuntimeStatusView): void
}

function sourceUrl(event: IpcMainInvokeEvent): string {
  return event.senderFrame?.url || event.sender.getURL()
}

function assertSource(event: IpcMainInvokeEvent, options: RegisterIpcOptions, appOnly = false, channel = ""): void {
  if (options.maintenanceFence.active && !options.maintenanceFence.allowsIpc(event, channel)) throw new Error("ipc_maintenance_document_rejected")
  const source = sourceUrl(event)
  const canonicalOrigin = options.getCanonicalOrigin()
  const trusted = appOnly
    ? canonicalOrigin !== null && isAllowedApplicationUrl(source, canonicalOrigin)
    : isTrustedIpcSource(source, canonicalOrigin, options.startupFile)
  if (!trusted) throw new Error("ipc_source_rejected")
  if (!options.maintenanceFence.active && CLEAR_DATA_CHANNELS.has(channel)) {
    const isOwnedStartupFile = new URL(source).protocol === "file:"
    if (!isOwnedStartupFile && !options.maintenanceFence.isAuthorizedDocument(event)) throw new Error("ipc_clear_document_rejected")
  }
}
async function showNativeMessage(event: IpcMainInvokeEvent, options: MessageBoxOptions): Promise<number> {
  const parent = BrowserWindow.fromWebContents(event.sender)
  const result = parent && !parent.isDestroyed() ? await dialog.showMessageBox(parent, options) : await dialog.showMessageBox(options)
  return result.response
}

async function confirmConfigApply(event: IpcMainInvokeEvent, preview: DesktopConfigPreviewResult): Promise<boolean> {
  return await showNativeMessage(event, {
    type: "warning", title: "确认应用 Research 运行配置", message: "应用已预览的运行配置？",
    detail: [
      `当前 revision：${preview.current?.revision ?? "未知"}；候选 revision：${preview.configuration?.revision ?? "未知"}`,
      `模型：${preview.configuration?.research.llm?.model ?? "未配置"}`,
      `模型地址：${preview.configuration?.research.llm?.baseUrl ?? "未配置"}`,
      `论文来源：${preview.configuration?.research.source.apiUrl ?? "未知"}`,
      `影响：${preview.effectScope?.llmConsumers.join(", ") || "无模型消费者"}`,
      "主进程将使用受控预览回执和 revision CAS；未知结果不会报告成功。",
      `预览有效期：${preview.expiresAt ?? "未知"}`
    ].join("\n"),
    buttons: ["取消", "应用配置"], defaultId: 0, cancelId: 0, noLink: true
  }) === 1
}

async function confirmRestore(event: IpcMainInvokeEvent, summary: RestoreNativeConfirmation): Promise<boolean> {
  const memory = summary.personalMemory
  const memoryDetail = memory
    ? [
        `个人记忆状态计数：${JSON.stringify(memory.statusCounts)}`,
        `个人记忆策略数：${memory.policyCount}`,
        memory.warning
      ].join("\n")
    : "该预览未包含个人记忆数据。"
  const options: MessageBoxOptions = {
    type: "warning",
    title: "确认激活恢复快照",
    message: "这会用已验证的旧快照替换当前本机数据",
    detail: [
      `备份标识：${summary.backupId}`,
      memoryDetail,
      "激活前会由既有 owner manager 停止或 drain 当前实例，不会强杀身份不明的进程。",
      "认证会话将按恢复策略旋转/失效；非终态工作会被 fence，不会被假称为已完成。",
      "默认保留当前数据。"
    ].join("\n"),
    buttons: ["保留当前数据", "激活已验证快照"],
    defaultId: 0,
    cancelId: 0,
    noLink: true
  }
  const parent = BrowserWindow.fromWebContents(event.sender)
  const result = parent && !parent.isDestroyed() ? await dialog.showMessageBox(parent, options) : await dialog.showMessageBox(options)
  return result.response === 1
}

async function confirmDataClear(event: IpcMainInvokeEvent, preview: DataClearPreviewResult): Promise<boolean> {
  const scope = preview.scope
  return await showNativeMessage(event, {
    type: "warning",
    title: "永久清除本机 Agora 数据",
    message: "确认清除此 App 当前 profile 的全部 owned 数据并退出？",
    detail: [
      `目标 profile：${scope?.profilePath ?? "未知"}`,
      `文件：${scope?.fileCount ?? "未知"}；总字节：${scope?.totalBytes ?? "未知"}`,
      `包括：${scope?.includes.join("；") ?? "未知"}`,
      `保留：${scope?.preserves.join("；") ?? "未知"}`,
      "你已在页面逐字输入固定确认短语。PREPARED 仅表示等待 finalizer；只有退出后完整删除并验证才会成为 CLEARED。",
      "默认取消；取消不会删除数据。"
    ].join("\n"),
    buttons: ["取消", "清除并退出"],
    defaultId: 0,
    cancelId: 0,
    noLink: true
  }) === 1
}
export function registerIpcHandlers(options: RegisterIpcOptions): () => void {
  ipcMain.handle(IPC_CHANNELS.runtimeStatus, async event => {
    assertSource(event, options, false, IPC_CHANNELS.runtimeStatus)
    const status = await options.coordinator.refresh()
    if (status.phase === "ready") options.onRuntimeReady(status)
    return status
  })
  ipcMain.handle(IPC_CHANNELS.runtimeRetry, async event => {
    assertSource(event, options, false, IPC_CHANNELS.runtimeStatus)
    const status = await options.coordinator.retry()
    if (status.phase === "ready") options.onRuntimeReady(status)
    return status
  })
  ipcMain.handle(IPC_CHANNELS.runtimeDiagnostics, event => {
    assertSource(event, options, false, IPC_CHANNELS.runtimeStatus)
    return options.coordinator.diagnostics
  })
  ipcMain.handle(IPC_CHANNELS.runtimeBackup, async event => {
    assertSource(event, options, true, "app-only")
    const parent = BrowserWindow.fromWebContents(event.sender)
    const dialogOptions: SaveDialogOptions = {
      title: "创建 Agora 本机备份",
      defaultPath: path.join(options.documentsPath, `agora-paper-agent-backup-${new Date().toISOString().slice(0, 10)}.zip`),
      buttonLabel: "创建备份",
      properties: ["showOverwriteConfirmation", "createDirectory"],
      filters: [{ name: "Agora backup", extensions: ["zip"] }]
    }
    const selected = parent && !parent.isDestroyed() ? await dialog.showSaveDialog(parent, dialogOptions) : await dialog.showSaveDialog(dialogOptions)
    if (selected.canceled || !selected.filePath) return { ok: false, state: "CANCELLED", code: "cancelled", message: "已取消备份。", cancelled: true }
    try {
      const destination = await assertSafeBackupTarget(selected.filePath)
      const result = await options.maintenance.backup(destination)
      await options.logger.write(result.ok ? "backup_completed" : "backup_failed", { code: result.code, fileName: path.basename(destination) })
      return result
    } catch (error) {
      const code = error instanceof NativePathValidationError ? error.code : "runtime_backup_failed"
      await options.logger.write("backup_failed", { code })
      return { ok: false, state: "ERROR", code, message: "所选备份目标不安全或不可用。" }
    }
  })
  ipcMain.handle(IPC_CHANNELS.runtimeRestorePreview, async event => {
    assertSource(event, options, true, "app-only")
    const parent = BrowserWindow.fromWebContents(event.sender)
    const dialogOptions: OpenDialogOptions = {
      title: "选择 Agora 备份进行隔离预览",
      buttonLabel: "验证备份",
      properties: ["openFile", "dontAddToRecent"],
      filters: [{ name: "Agora backup", extensions: ["zip"] }]
    }
    const selected = parent && !parent.isDestroyed() ? await dialog.showOpenDialog(parent, dialogOptions) : await dialog.showOpenDialog(dialogOptions)
    if (selected.canceled || selected.filePaths.length !== 1) return { ok: false, state: "CANCELLED", code: "cancelled", message: "已取消恢复预览。", cancelled: true }
    try {
      const source = await assertSafeBackupSource(selected.filePaths[0])
      const result = await options.maintenance.preview(source)
      await options.logger.write(result.ok ? "restore_preview_verified" : "restore_preview_failed", { code: result.code, backupId: result.backupId ?? null })
      return result
    } catch (error) {
      const code = error instanceof NativePathValidationError ? error.code : "runtime_restore_preview_failed"
      await options.logger.write("restore_preview_failed", { code })
      return { ok: false, state: "ERROR", code, message: "所选备份文件不安全或不可用。" }
    }
  })
  ipcMain.handle(IPC_CHANNELS.runtimeRestoreActivate, async (event, value: unknown) => {
    assertSource(event, options, true, "app-only")
    const result = await options.maintenance.activate(value, summary => confirmRestore(event, summary))
    await options.logger.write(result.ok ? "restore_activated" : "restore_activation_not_completed", { code: result.code, backupId: result.backupId ?? null })
    return result
  })
  ipcMain.handle(IPC_CHANNELS.runtimeClearDataPreview, async event => {
    assertSource(event, options, false, IPC_CHANNELS.runtimeClearDataPreview)
    if (!options.maintenanceFence.enter(event)) throw new Error("ipc_maintenance_document_rejected")
    const result = await options.clearData.preview()
    await options.logger.write(result.ok ? "data_clear_preview_ready" : "data_clear_preview_failed", {
      code: result.code,
      state: result.state,
      fileCount: result.scope?.fileCount ?? null,
      totalBytes: result.scope?.totalBytes ?? null,
      stopped: result.stopped === true
    })
    return result
  })
  ipcMain.handle(IPC_CHANNELS.runtimeClearDataApply, async (event, value: unknown) => {
    assertSource(event, options, false, IPC_CHANNELS.runtimeClearDataApply)
    const parent = BrowserWindow.fromWebContents(event.sender)
    const freshLifecycle = async () => {
      if (!parent || parent.isDestroyed()) return false
      const assessment = await options.lifecycle.requestQuit(parent, "data-clear")
      if (assessment.status === "confirmed" && assessment.decision === "cancel") return false
      if (!requiresNativeQuitConfirmation(assessment)) return true
      const detail = assessment.status === "unknown"
        ? "页面未返回可信的新鲜工作状态。默认取消；继续可能永久丢失未知草稿或中断未知任务。"
        : `页面报告 ${assessment.state.activeTaskCount} 个活动任务，且未保存状态为 ${assessment.state.dirty ? "是" : "否"}。继续会永久清除当前 profile。`
      return await showNativeMessage(event, {
        type: "warning",
        title: "当前工作需要确认",
        message: "仍然继续永久清除本机数据？",
        detail,
        buttons: ["取消", "仍然继续"],
        defaultId: 0,
        cancelId: 0,
        noLink: true
      }) === 1
    }
    const result = await options.clearData.apply(value, freshLifecycle, preview => confirmDataClear(event, preview))
    await options.logger.write(result.ok ? "data_clear_prepared" : "data_clear_not_prepared", { code: result.code, state: result.state, operationId: result.operationId ?? null, exitRequired: result.exitRequired === true })
    return result
  })
  ipcMain.handle(IPC_CHANNELS.runtimeClearDataStatus, async event => {
    assertSource(event, options, false, IPC_CHANNELS.runtimeClearDataStatus)
    const result = await options.clearData.status()
    if (!result.ok || result.state === "FAILED_PARTIAL") await options.logger.write("data_clear_status_blocked", { code: result.code, state: result.state, operationId: result.operationId ?? null })
    return result
  })
  ipcMain.handle(IPC_CHANNELS.configShow, async event => {
    assertSource(event, options, true, "app-only")
    return await options.config.show()
  })
  ipcMain.handle(IPC_CHANNELS.configPreview, async (event, value: unknown) => {
    assertSource(event, options, true, "app-only")
    return await options.config.preview(value)
  })
  ipcMain.handle(IPC_CHANNELS.configApply, async (event, value: unknown) => {
    assertSource(event, options, true, "app-only")
    const parent = BrowserWindow.fromWebContents(event.sender)
    const freshLifecycle = async () => {
      if (!parent || parent.isDestroyed()) return false
      const assessment = await options.lifecycle.requestQuit(parent, "config-apply")
      if (assessment.status === "confirmed" && assessment.decision === "cancel") return false
      if (!requiresNativeQuitConfirmation(assessment)) return true
      const detail = assessment.status === "unknown"
        ? "页面未返回可信的新鲜工作状态。继续可能中断未知工作；取消不会停止服务或应用配置。"
        : `页面报告 ${assessment.state.activeTaskCount} 个活动任务，且未保存状态为 ${assessment.state.dirty ? "是" : "否"}。继续可能中断这些工作。`
      return await showNativeMessage(event, {
        type: "warning", title: "当前工作需要确认", message: "仍然继续核对并应用配置？",
        detail, buttons: ["取消", "仍然继续"], defaultId: 0, cancelId: 0, noLink: true
      }) === 1
    }
    const result = await options.config.apply(value, freshLifecycle, preview => confirmConfigApply(event, preview))
    await options.logger.write(result.ok ? "runtime_config_applied" : "runtime_config_not_applied", { code: result.code, state: result.state })
    return result
  })
  ipcMain.handle(IPC_CHANNELS.credentialStatus, async event => {
    assertSource(event, options, true, "app-only")
    return await options.credentials.invoke("status")
  })
  ipcMain.handle(IPC_CHANNELS.credentialReplace, async event => {
    assertSource(event, options, true, "app-only")
    const parent = BrowserWindow.fromWebContents(event.sender)
    if (!parent || parent.isDestroyed()) return { ok: false, code: "desktop_credential_parent_invalid", configured: null, state: "UNKNOWN" }
    const fresh = await options.coordinator.refresh()
    if (fresh.phase !== "stopped") {
      const accepted = await showNativeMessage(event, { type: "info", title: "替换 Research 密钥", message: "新密钥仅在下次启动 API 时生效。", detail: "当前 API 仍可能持有旧密钥。取消录入不会停止服务或更改密钥；成功录入也不会自动重启。", buttons: ["取消", "打开原生密钥录入"], defaultId: 0, cancelId: 0, noLink: true }) === 1
      if (!accepted) {
        const current = await options.credentials.invoke("status")
        return { ...current, ok: current.ok, code: "credential_cancelled", changed: false, cancelled: true }
      }
    }
    const handle = parent.getNativeWindowHandle()
    const parentWindow = handle.length >= 8 ? handle.readBigUInt64LE(0) : handle.length >= 4 ? BigInt(handle.readUInt32LE(0)) : 0n
    const result = await options.credentials.invoke("replace", parentWindow)
    await options.logger.write(result.ok && !result.cancelled ? "credential_replaced" : "credential_not_replaced", { code: result.code, configured: result.configured, cancelled: result.cancelled === true })
    return result
  })
  ipcMain.handle(IPC_CHANNELS.credentialRemove, async event => {
    assertSource(event, options, true, "app-only")
    const confirmed = await showNativeMessage(event, { type: "warning", title: "删除 Research 密钥", message: "从 Windows Credential Manager 删除本 App 的 Research 密钥？", detail: "删除前 owner 将验证并停止本 App 服务；本地阅读仍可使用，远程模型能力将受限。", buttons: ["保留密钥", "删除密钥"], defaultId: 0, cancelId: 0, noLink: true }) === 1
    if (!confirmed) {
      const current = await options.credentials.invoke("status")
      return { ...current, ok: current.ok, code: "credential_cancelled", changed: false, cancelled: true }
    }
    const parent = BrowserWindow.fromWebContents(event.sender)
    if (!parent || parent.isDestroyed()) return { ok: false, code: "desktop_credential_parent_invalid", configured: null, state: "UNKNOWN" }
    const assessment = await options.lifecycle.requestQuit(parent, "config-apply")
    if (assessment.status === "confirmed" && assessment.decision === "cancel") {
      const current = await options.credentials.invoke("status")
      return { ...current, ok: current.ok, code: "credential_cancelled", changed: false, cancelled: true }
    }
    if (requiresNativeQuitConfirmation(assessment)) {
      const accepted = await showNativeMessage(event, { type: "warning", title: "确认中断当前工作", message: "删除密钥前需要停止本 App 的服务。", detail: assessment.status === "unknown" ? "页面工作状态未知。默认取消；继续可能中断未知工作。" : `活动任务 ${assessment.state.activeTaskCount}；未保存状态：${assessment.state.dirty ? "是" : "否"}。`, buttons: ["取消", "仍然继续"], defaultId: 0, cancelId: 0, noLink: true }) === 1
      if (!accepted) {
        const current = await options.credentials.invoke("status")
        return { ...current, ok: current.ok, code: "credential_cancelled", changed: false, cancelled: true }
      }
    }
    const result = await options.credentials.invoke("delete")
    await options.logger.write(result.ok ? "credential_deleted" : "credential_not_deleted", { code: result.code, configured: result.configured })
    return result
  })
  ipcMain.handle(IPC_CHANNELS.lifecycleUpdate, (event, state: unknown) => {
    assertSource(event, options, false, IPC_CHANNELS.lifecycleUpdate)
    return options.lifecycle.update(state)
  })
  ipcMain.handle(IPC_CHANNELS.lifecycleRespond, (event, response: unknown) => {
    assertSource(event, options, false, IPC_CHANNELS.lifecycleRespond)
    return options.lifecycle.respond(response)
  })
  ipcMain.handle(IPC_CHANNELS.exportSave, async (event, value: unknown) => {
    assertSource(event, options, true, "app-only")
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

  return () => {
    for (const channel of Object.values(IPC_CHANNELS)) {
      if (channel !== IPC_CHANNELS.lifecycleQuitRequested) ipcMain.removeHandler(channel)
    }
  }
}












