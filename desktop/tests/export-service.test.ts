import { mkdir, mkdtemp, readFile, symlink, writeFile } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { describe, expect, it } from "vitest"
import { MAX_EXPORT_BYTES, assertSafeExportTarget, validateExportRequest, writeExportFile } from "../src/export-service"

describe("export service", () => {
  it("accepts only bounded txt/md/json content and controlled file names", () => {
    expect(validateExportRequest({ suggestedName: "研究笔记.md", format: "md", content: "# Note" }).fileName).toBe("研究笔记.md")
    expect(() => validateExportRequest({ suggestedName: "../secret.md", format: "md", content: "x" })).toThrow(/path/)
    expect(() => validateExportRequest({ suggestedName: "result.json", format: "json", content: "not-json" })).toThrow(/valid JSON/)
    expect(() => validateExportRequest({ suggestedName: "large.txt", format: "txt", content: "x".repeat(MAX_EXPORT_BYTES + 1) })).toThrow(/5 MiB/)
  })

  it("writes a selected regular file and flushes it", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "agora-export-"))
    const target = path.join(root, "notes.md")
    const request = validateExportRequest({ suggestedName: "notes.md", format: "md", content: "safe" })
    await writeExportFile(target, request)
    expect(await readFile(target, "utf8")).toBe("safe")
  })

  it("refuses to overwrite an existing target so a link swap cannot truncate another file", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "agora-export-existing-"))
    const target = path.join(root, "notes.txt")
    await writeFile(target, "original")
    const request = validateExportRequest({ suggestedName: "notes.txt", format: "txt", content: "replacement" })
    await expect(writeExportFile(target, request)).rejects.toThrow(/already exists/)
    expect(await readFile(target, "utf8")).toBe("original")
  })

  it("rejects linked target directories", async () => {
    const root = await mkdtemp(path.join(os.tmpdir(), "agora-export-link-"))
    const real = path.join(root, "real")
    const linked = path.join(root, "linked")
    await mkdir(real)
    try {
      await symlink(real, linked, process.platform === "win32" ? "junction" : "dir")
    } catch {
      return
    }
    await expect(assertSafeExportTarget(path.join(linked, "notes.txt"), "txt")).rejects.toThrow(/link|junction|regular directory/)
  })
})
