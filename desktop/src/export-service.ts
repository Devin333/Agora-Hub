import { constants } from "node:fs"
import { lstat, open, realpath } from "node:fs/promises"
import path from "node:path"
import type { ExportFormat, ExportRequest, ExportResult } from "./contracts"

export const MAX_EXPORT_BYTES = 5 * 1024 * 1024
const EXTENSIONS: Record<ExportFormat, string> = { txt: ".txt", md: ".md", json: ".json" }

export class ExportValidationError extends Error {
  constructor(readonly code: "invalid_input" | "unsafe_target", message: string) {
    super(message)
    this.name = "ExportValidationError"
  }
}

export interface ValidatedExport {
  fileName: string
  format: ExportFormat
  content: string
  byteLength: number
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value)
}

export function validateExportRequest(value: unknown): ValidatedExport {
  if (!isRecord(value) || (value.format !== "txt" && value.format !== "md" && value.format !== "json") || typeof value.suggestedName !== "string" || typeof value.content !== "string") {
    throw new ExportValidationError("invalid_input", "Export request is invalid")
  }
  if (/[/\\\0\r\n]/.test(value.suggestedName) || value.suggestedName === "." || value.suggestedName === "..") {
    throw new ExportValidationError("invalid_input", "Export file name must not contain a path")
  }
  const extension = EXTENSIONS[value.format]
  const stem = path.basename(value.suggestedName, path.extname(value.suggestedName)).trim()
  if (!stem || stem.length > 100 || !/^[\p{L}\p{N} _().\-]+$/u.test(stem)) {
    throw new ExportValidationError("invalid_input", "Export file name contains unsupported characters")
  }
  if (value.content.includes("\0")) throw new ExportValidationError("invalid_input", "Export content contains a NUL byte")
  const byteLength = Buffer.byteLength(value.content, "utf8")
  if (byteLength > MAX_EXPORT_BYTES) throw new ExportValidationError("invalid_input", "Export content exceeds the 5 MiB limit")
  if (value.format === "json") {
    try {
      JSON.parse(value.content)
    } catch {
      throw new ExportValidationError("invalid_input", "JSON export content must be valid JSON")
    }
  }
  return { fileName: `${stem}${extension}`, format: value.format, content: value.content, byteLength }
}

export async function assertSafeExportTarget(targetPath: string, format: ExportFormat): Promise<void> {
  if (!path.isAbsolute(targetPath) || path.extname(targetPath).toLowerCase() !== EXTENSIONS[format]) {
    throw new ExportValidationError("unsafe_target", "Export target must be an absolute path with the selected extension")
  }
  const parent = path.dirname(targetPath)
  let parentInfo
  try {
    parentInfo = await lstat(parent)
  } catch {
    throw new ExportValidationError("unsafe_target", "Export target directory does not exist")
  }
  if (!parentInfo.isDirectory() || parentInfo.isSymbolicLink()) {
    throw new ExportValidationError("unsafe_target", "Export target directory is not a regular directory")
  }
  const canonicalParent = await realpath(parent)
  if (process.platform === "win32" ? canonicalParent.toLowerCase() !== path.resolve(parent).toLowerCase() : canonicalParent !== path.resolve(parent)) {
    throw new ExportValidationError("unsafe_target", "Export target directory must not traverse a link or junction")
  }
  try {
    const targetInfo = await lstat(targetPath)
    if (!targetInfo.isFile() || targetInfo.isSymbolicLink()) {
      throw new ExportValidationError("unsafe_target", "Export target is not a regular file")
    }
    throw new ExportValidationError("unsafe_target", "Export target already exists; choose a new file name")
  } catch (error) {
    if (error instanceof ExportValidationError) throw error
    const code = (error as NodeJS.ErrnoException).code
    if (code !== "ENOENT") throw new ExportValidationError("unsafe_target", "Export target could not be inspected")
  }
}

export async function writeExportFile(targetPath: string, request: ValidatedExport): Promise<void> {
  await assertSafeExportTarget(targetPath, request.format)
  const noFollow = constants.O_NOFOLLOW ?? 0
  const flags = constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | noFollow
  const handle = await open(targetPath, flags, 0o600)
  try {
    const info = await handle.stat()
    if (!info.isFile()) throw new ExportValidationError("unsafe_target", "Export target changed before writing")
    await handle.writeFile(request.content, { encoding: "utf8" })
    await handle.sync()
  } finally {
    await handle.close()
  }
}

export function exportFailure(error: unknown): ExportResult {
  if (error instanceof ExportValidationError) return { ok: false, code: error.code, message: error.message }
  return { ok: false, code: "write_failed", message: "Export could not be written" }
}
