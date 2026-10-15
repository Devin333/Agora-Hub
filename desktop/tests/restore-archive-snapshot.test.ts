import { mkdtemp, readFile, writeFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { describe, expect, it } from "vitest"
import { RestoreArchiveSnapshotStore } from "../src/restore-archive-snapshot"

describe("RestoreArchiveSnapshotStore", () => {
  it("copies a selected archive into an owned snapshot and binds its full SHA-256", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "agora-restore-snapshot-"))
    const source = path.join(root, "source.zip")
    const snapshots = path.join(root, "private")
    await writeFile(source, Buffer.from("stable archive bytes"))
    const store = new RestoreArchiveSnapshotStore(snapshots)
    const snapshot = await store.create(source)
    expect(snapshot.path).toMatch(/private[\\/][0-9a-f-]+\.zip$/i)
    expect(snapshot.sha256).toMatch(/^[0-9a-f]{64}$/)
    expect(await readFile(snapshot.path, "utf8")).toBe("stable archive bytes")
    await expect(store.verify(snapshot)).resolves.toBe(true)
  })

  it("rejects a snapshot changed after preview instead of trusting old counts or backup id", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "agora-restore-snapshot-"))
    const source = path.join(root, "source.zip")
    await writeFile(source, Buffer.from("original"))
    const store = new RestoreArchiveSnapshotStore(path.join(root, "private"))
    const snapshot = await store.create(source)
    await writeFile(snapshot.path, Buffer.from("replaced archive with same claimed identity"))
    await expect(store.verify(snapshot)).resolves.toBe(false)
  })
})
