import { afterEach, describe, expect, it, vi } from "vitest"
import { saveUserExport } from "./user-export"

afterEach(() => {
  delete (window as unknown as { agoraDesktop?: unknown }).agoraDesktop
  vi.restoreAllMocks()
})

describe("user export bridge", () => {
  it("sends exact bounded content to the native save bridge", async () => {
    const save = vi.fn().mockResolvedValue({ ok: true, code: "saved", fileName: "notes.md", message: "saved" })
    Object.assign(window, { agoraDesktop: { version: 1, export: { save } } })
    const result = await saveUserExport({ suggestedName: "Paper notes", format: "md", content: "# Notes\nExact content" })
    expect(save).toHaveBeenCalledWith({ suggestedName: "Paper notes", format: "md", content: "# Notes\nExact content" })
    expect(result).toMatchObject({ ok: true, code: "saved" })
  })


  it("normalizes unsupported and overlong native names without changing content", async () => {
    const save = vi.fn().mockResolvedValue({ ok: true, code: "saved", fileName: "saved.md", message: "saved" })
    Object.assign(window, { agoraDesktop: { version: 1, export: { save } } })
    const content = "中文与 English 原文保持不变"
    await saveUserExport({ suggestedName: `${"很长的论文标题".repeat(20)}: notes.md`, format: "md", content })
    const request = save.mock.calls[0][0]
    expect([...request.suggestedName].length).toBeLessThanOrEqual(100)
    expect(request.suggestedName).toMatch(/^[\p{L}\p{N} _().-]+$/u)
    expect(request.suggestedName).not.toContain(":")
    expect(request.content).toBe(content)
  })

  it("turns a rejected native IPC promise into an unknown write state", async () => {
    const save = vi.fn().mockRejectedValue(new Error("ipc disconnected"))
    Object.assign(window, { agoraDesktop: { version: 1, export: { save } } })
    await expect(saveUserExport({ suggestedName: "notes", format: "md", content: "draft" })).resolves.toMatchObject({
      ok: false,
      code: "write_failed",
      message: expect.stringContaining("无法确认"),
    })
  })

  it("does not fall back to browser download when the native dialog is cancelled", async () => {
    const save = vi.fn().mockResolvedValue({ ok: false, code: "cancelled", message: "cancelled" })
    Object.assign(window, { agoraDesktop: { version: 1, export: { save } } })
    const createObjectURL = vi.spyOn(URL, "createObjectURL")
    await expect(saveUserExport({ suggestedName: "memory", format: "json", content: "{}" })).resolves.toMatchObject({ code: "cancelled" })
    expect(createObjectURL).not.toHaveBeenCalled()
  })


  it("reports only a browser download request and normalizes an existing extension", async () => {
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined)
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:test")
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => undefined)
    const result = await saveUserExport({ suggestedName: "memory.json", format: "json", content: "{}" })
    expect(result).toMatchObject({ ok: true, code: "download_started", fileName: "memory.json" })
    expect(result.message).toContain("发起下载")
    expect(click).toHaveBeenCalledOnce()
  })
  it("rejects content above the host limit before invoking native save", async () => {
    const save = vi.fn()
    Object.assign(window, { agoraDesktop: { version: 1, export: { save } } })
    await expect(saveUserExport({ suggestedName: "large", format: "txt", content: "x".repeat(5 * 1024 * 1024 + 1) })).resolves.toMatchObject({ code: "invalid_input" })
    expect(save).not.toHaveBeenCalled()
  })
})

