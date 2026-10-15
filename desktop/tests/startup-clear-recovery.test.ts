import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"

const sourceRoot = path.resolve(__dirname, "../src")

describe("owned startup clear recovery", () => {
  it("is a local content-free recovery surface with the frozen authorization flow", () => {
    const html = readFileSync(path.join(sourceRoot, "startup.html"), "utf8")
    const script = readFileSync(path.join(sourceRoot, "startup.js"), "utf8")

    expect(html).toContain("connect-src 'none'")
    expect(html).toContain('<script src="./startup.js"></script>')
    expect(html).not.toMatch(/<script[^>]+src=["']https?:/i)
    expect(html).toContain('id="clear-preview"')
    expect(html).toContain('id="clear-apply"')
    expect(script).toContain('const CLEAR_PHRASE = "删除本机全部 Agora 数据"')
    expect(script).toContain("runtime.clearData.preview()")
    expect(script).toContain("runtime.clearData.apply({ previewId: currentPreviewId, confirmationText: clearConfirmation.value })")
    expect(script).toContain('!clearState.ok || ["PREPARED", "FINALIZING", "FAILED_PARTIAL"].includes(clearState.state)')
  })
})
