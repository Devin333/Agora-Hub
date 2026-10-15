export type UserExportFormat = "txt" | "md" | "json"
export type UserExportResult = {
  ok: boolean
  code: "saved" | "cancelled" | "invalid_input" | "unsafe_target" | "write_failed"
  fileName?: string
  message: string
}

type DesktopExportBridge = {
  save(request: { suggestedName: string; format: UserExportFormat; content: string }): Promise<UserExportResult>
}
type ExportWindow = Window & { agoraDesktop?: { version: 1; export: DesktopExportBridge } }

const MAX_EXPORT_BYTES = 5 * 1024 * 1024

export async function saveUserExport(request: { suggestedName: string; format: UserExportFormat; content: string }): Promise<UserExportResult | { ok: true; code: "download_started"; fileName: string; message: string }> {
  if (!request.content || new TextEncoder().encode(request.content).byteLength > MAX_EXPORT_BYTES) {
    return { ok: false, code: "invalid_input", message: "导出内容为空或超过桌面宿主 5 MiB 上限。" }
  }
  const stem = safeExportStem(request.suggestedName, request.format)
  const bridge = (window as ExportWindow).agoraDesktop?.export
  if (bridge) {
    try {
      return await bridge.save({ ...request, suggestedName: stem })
    } catch (cause: unknown) {
      void cause
      return {
        ok: false,
        code: "write_failed",
        message: "无法确认桌面导出是否完成；当前内容仍保留，请检查桌面状态后重试。",
      }
    }
  }

  const blob = new Blob([request.content], { type: mediaType(request.format) })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = `${stem}.${request.format}`
  anchor.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
  return { ok: true, code: "download_started", fileName: anchor.download, message: "浏览器已发起下载，请在下载记录中确认文件已落盘。" }
}

function safeExportStem(value: string, format: UserExportFormat): string {
  const withoutKnownExtension = value.trim().replace(new RegExp(`\\.${format}$`, "i"), "")
  const normalized = withoutKnownExtension
    .normalize("NFKC")
    .replace(/[^\p{L}\p{N} _().-]+/gu, "-")
    .replace(/\s+/g, " ")
    .replace(/^[. -]+|[. -]+$/g, "")
  return [...normalized].slice(0, 100).join("") || "agora-export"
}

function mediaType(format: UserExportFormat): string {
  if (format === "json") return "application/json;charset=utf-8"
  return format === "md" ? "text/markdown;charset=utf-8" : "text/plain;charset=utf-8"
}

