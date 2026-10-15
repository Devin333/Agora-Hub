import { createHash, randomUUID } from "node:crypto"
import type { Stats } from "node:fs"
import { mkdir, lstat, open, realpath, rm } from "node:fs/promises"
import path from "node:path"

export const MAX_RESTORE_ARCHIVE_BYTES = 50 * 1024 * 1024 * 1024
const CHUNK_BYTES = 1024 * 1024

export interface RestoreArchiveSnapshot {
  path: string
  sha256: string
  size: number
}

export interface RestoreArchiveSnapshots {
  create(sourcePath: string): Promise<RestoreArchiveSnapshot>
  verify(snapshot: RestoreArchiveSnapshot): Promise<boolean>
  remove(snapshot: RestoreArchiveSnapshot): Promise<void>
}

function equalPath(left: string, right: string): boolean {
  return process.platform === "win32" ? left.toLowerCase() === right.toLowerCase() : left === right
}

function sameFile(left: Stats, right: Stats): boolean {
  return left.dev === right.dev && left.ino === right.ino && left.size === right.size && left.mtimeMs === right.mtimeMs && left.ctimeMs === right.ctimeMs
}

export class RestoreArchiveSnapshotStore implements RestoreArchiveSnapshots {
  constructor(private readonly root: string) {}

  async create(sourcePath: string): Promise<RestoreArchiveSnapshot> {
    await this.ensureRoot()
    const canonicalSource = await realpath(sourcePath)
    if (!equalPath(canonicalSource, path.resolve(sourcePath))) throw new Error("restore_archive_source_changed")
    const sourceInfo = await lstat(sourcePath)
    if (!sourceInfo.isFile() || sourceInfo.isSymbolicLink() || sourceInfo.size > MAX_RESTORE_ARCHIVE_BYTES) throw new Error("restore_archive_source_invalid")

    const destination = path.join(this.root, `${randomUUID()}.zip`)
    const source = await open(sourcePath, "r")
    const target = await open(destination, "wx", 0o600)
    const digest = createHash("sha256")
    let written = 0
    try {
      const before = await source.stat()
      if (!before.isFile() || before.size > MAX_RESTORE_ARCHIVE_BYTES) throw new Error("restore_archive_source_invalid")
      const buffer = Buffer.allocUnsafe(CHUNK_BYTES)
      while (true) {
        const { bytesRead } = await source.read(buffer, 0, buffer.length, null)
        if (bytesRead === 0) break
        written += bytesRead
        if (written > before.size || written > MAX_RESTORE_ARCHIVE_BYTES) throw new Error("restore_archive_source_changed")
        const chunk = buffer.subarray(0, bytesRead)
        digest.update(chunk)
        let offset = 0
        while (offset < chunk.length) {
          const { bytesWritten } = await target.write(chunk, offset, chunk.length - offset, null)
          if (bytesWritten <= 0) throw new Error("restore_archive_snapshot_write_failed")
          offset += bytesWritten
        }
      }
      const after = await source.stat()
      if (written !== before.size || !sameFile(before, after)) throw new Error("restore_archive_source_changed")
      await target.sync()
      const pathInfo = await lstat(sourcePath)
      if (!sameFile(after, pathInfo)) throw new Error("restore_archive_source_changed")
    } catch (error) {
      await target.close().catch(() => undefined)
      await source.close().catch(() => undefined)
      await rm(destination, { force: true }).catch(() => undefined)
      throw error
    }
    await target.close()
    await source.close()
    const snapshot = { path: destination, sha256: digest.digest("hex"), size: written }
    if (!(await this.verify(snapshot))) {
      await this.remove(snapshot)
      throw new Error("restore_archive_snapshot_invalid")
    }
    return snapshot
  }

  async verify(snapshot: RestoreArchiveSnapshot): Promise<boolean> {
    try {
      await this.ensureRoot()
      const resolved = path.resolve(snapshot.path)
      const relative = path.relative(path.resolve(this.root), resolved)
      if (!relative || relative.startsWith(`..${path.sep}`) || relative === ".." || path.isAbsolute(relative)) return false
      const info = await lstat(resolved)
      if (!info.isFile() || info.isSymbolicLink() || info.size !== snapshot.size || info.size > MAX_RESTORE_ARCHIVE_BYTES) return false
      if (!equalPath(await realpath(resolved), resolved)) return false
      const handle = await open(resolved, "r")
      const digest = createHash("sha256")
      let read = 0
      try {
        const before = await handle.stat()
        const buffer = Buffer.allocUnsafe(CHUNK_BYTES)
        while (true) {
          const { bytesRead } = await handle.read(buffer, 0, buffer.length, null)
          if (bytesRead === 0) break
          read += bytesRead
          if (read > snapshot.size) return false
          digest.update(buffer.subarray(0, bytesRead))
        }
        const after = await handle.stat()
        if (!sameFile(before, after)) return false
      } finally {
        await handle.close()
      }
      return read === snapshot.size && digest.digest("hex") === snapshot.sha256
    } catch {
      return false
    }
  }

  async remove(snapshot: RestoreArchiveSnapshot): Promise<void> {
    await rm(snapshot.path, { force: true }).catch(() => undefined)
  }

  private async ensureRoot(): Promise<void> {
    await mkdir(this.root, { recursive: true, mode: 0o700 })
    const info = await lstat(this.root)
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("restore_snapshot_root_unsafe")
    if (!equalPath(await realpath(this.root), path.resolve(this.root))) throw new Error("restore_snapshot_root_unsafe")
  }
}
