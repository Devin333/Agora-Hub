import { lstat, realpath } from "node:fs/promises"
import path from "node:path"

export class NativePathValidationError extends Error {
  constructor(readonly code: "unsafe_backup_target" | "unsafe_backup_source", message: string) {
    super(message)
    this.name = "NativePathValidationError"
  }
}

function equalPath(left: string, right: string): boolean {
  return process.platform === "win32" ? left.toLowerCase() === right.toLowerCase() : left === right
}

async function assertPlainDirectory(directory: string, code: NativePathValidationError["code"]): Promise<void> {
  let info
  try { info = await lstat(directory) }
  catch { throw new NativePathValidationError(code, "Selected directory does not exist") }
  if (!info.isDirectory() || info.isSymbolicLink()) throw new NativePathValidationError(code, "Selected directory is not a plain directory")
  const canonical = await realpath(directory)
  if (!equalPath(canonical, path.resolve(directory))) throw new NativePathValidationError(code, "Selected path traverses a link or junction")
}

export async function assertSafeBackupTarget(targetPath: string): Promise<string> {
  if (!path.isAbsolute(targetPath) || path.extname(targetPath).toLowerCase() !== ".zip" || /[\0\r\n]/.test(targetPath)) {
    throw new NativePathValidationError("unsafe_backup_target", "Backup target must be an absolute .zip path")
  }
  const resolved = path.resolve(targetPath)
  await assertPlainDirectory(path.dirname(resolved), "unsafe_backup_target")
  try {
    await lstat(resolved)
    throw new NativePathValidationError("unsafe_backup_target", "Backup target already exists; choose a new file name")
  } catch (error) {
    if (error instanceof NativePathValidationError) throw error
    if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw new NativePathValidationError("unsafe_backup_target", "Backup target could not be inspected")
  }
  return resolved
}

export async function assertSafeBackupSource(sourcePath: string): Promise<string> {
  if (!path.isAbsolute(sourcePath) || path.extname(sourcePath).toLowerCase() !== ".zip" || /[\0\r\n]/.test(sourcePath)) {
    throw new NativePathValidationError("unsafe_backup_source", "Restore source must be an absolute .zip path")
  }
  const resolved = path.resolve(sourcePath)
  await assertPlainDirectory(path.dirname(resolved), "unsafe_backup_source")
  let info
  try { info = await lstat(resolved) }
  catch { throw new NativePathValidationError("unsafe_backup_source", "Selected backup does not exist") }
  if (!info.isFile() || info.isSymbolicLink()) throw new NativePathValidationError("unsafe_backup_source", "Selected backup is not a plain file")
  const canonical = await realpath(resolved)
  if (!equalPath(canonical, resolved)) throw new NativePathValidationError("unsafe_backup_source", "Selected backup traverses a link or junction")
  return resolved
}
