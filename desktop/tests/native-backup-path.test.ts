import { mkdir, mkdtemp, writeFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { describe, expect, it } from "vitest"
import { assertSafeBackupSource, assertSafeBackupTarget } from "../src/native-backup-path"

describe("native backup path validation", () => {
  it("accepts an absent zip target under a real directory", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "agora-backup-path-"))
    await expect(assertSafeBackupTarget(path.join(root, "backup.zip"))).resolves.toBe(path.join(root, "backup.zip"))
  })

  it("accepts only an existing plain zip source", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "agora-backup-path-"))
    const source = path.join(root, "backup.zip")
    await writeFile(source, "fixture")
    await expect(assertSafeBackupSource(source)).resolves.toBe(source)
    await mkdir(path.join(root, "not-a-file.zip"))
    await expect(assertSafeBackupSource(path.join(root, "not-a-file.zip"))).rejects.toMatchObject({ code: "unsafe_backup_source" })
  })

  it("rejects wrong extensions and existing backup targets", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "agora-backup-path-"))
    const existing = path.join(root, "existing.zip")
    await writeFile(existing, "fixture")
    await expect(assertSafeBackupTarget(existing)).rejects.toMatchObject({ code: "unsafe_backup_target" })
    await expect(assertSafeBackupSource(path.join(root, "backup.txt"))).rejects.toMatchObject({ code: "unsafe_backup_source" })
  })
})
