import { lstatSync, realpathSync } from "node:fs"
import path from "node:path"

const TEST_USER_DATA_PREFIX = "--agora-test-user-data-dir="

export interface UserDataOverrideInput {
  argv: readonly string[]
  isPackaged: boolean
  testMode: boolean
}

export function resolveTestUserDataOverride(input: UserDataOverrideInput): string | null {
  const values = input.argv.filter(argument => argument.startsWith(TEST_USER_DATA_PREFIX))
  if (values.length === 0) return null
  if (input.isPackaged || !input.testMode || values.length !== 1) throw new Error("Test userData override is not allowed")

  const candidate = values[0].slice(TEST_USER_DATA_PREFIX.length)
  if (!candidate || candidate.includes("\0") || !path.isAbsolute(candidate) || path.normalize(candidate) !== candidate) {
    throw new Error("Test userData override must be one normalized absolute path")
  }
  const info = lstatSync(candidate)
  if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("Test userData override must be an existing plain directory")
  const resolved = path.resolve(candidate)
  const canonical = realpathSync.native(candidate)
  if (process.platform === "win32" ? canonical.toLowerCase() !== resolved.toLowerCase() : canonical !== resolved) {
    throw new Error("Test userData override must not traverse links or junctions")
  }
  return resolved
}
