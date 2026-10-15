import { mkdtemp } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { describe, expect, it } from "vitest"
import { resolveTestUserDataOverride } from "../src/user-data-override"

describe("test userData override", () => {
  it("accepts one existing plain absolute directory only in unpackaged test mode", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "agora-user-data-"))
    expect(resolveTestUserDataOverride({
      argv: [`--agora-test-user-data-dir=${directory}`],
      isPackaged: false,
      testMode: true
    })).toBe(path.resolve(directory))
  })

  it("keeps production defaults unchanged and rejects attempts to enable the override outside test mode", async () => {
    const directory = await mkdtemp(path.join(os.tmpdir(), "agora-user-data-"))
    expect(resolveTestUserDataOverride({ argv: [], isPackaged: false, testMode: false })).toBeNull()
    expect(() => resolveTestUserDataOverride({
      argv: [`--agora-test-user-data-dir=${directory}`],
      isPackaged: false,
      testMode: false
    })).toThrow("not allowed")
    expect(() => resolveTestUserDataOverride({
      argv: [`--agora-test-user-data-dir=${directory}`],
      isPackaged: true,
      testMode: true
    })).toThrow("not allowed")
  })

  it("rejects relative or non-normalized paths", () => {
    expect(() => resolveTestUserDataOverride({
      argv: ["--agora-test-user-data-dir=.\\relative"],
      isPackaged: false,
      testMode: true
    })).toThrow("normalized absolute path")
  })
})
