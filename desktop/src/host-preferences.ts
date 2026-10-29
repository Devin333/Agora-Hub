import { randomUUID } from "node:crypto"
import { readFileSync } from "node:fs"
import { mkdir, rename, rm, writeFile } from "node:fs/promises"
import path from "node:path"

/**
 * Small host-owned preference documents (appearance, server connection).
 *
 * Every document lives in one userData directory, carries a schema tag so a
 * foreign or older file is ignored instead of misread, and is replaced
 * atomically so a crash never leaves a torn document behind.
 */
export const HOST_PREFERENCES_DIRECTORY = "host-preferences"

export function hostPreferencePath(userDataPath: string, fileName: string): string {
  return path.join(userDataPath, HOST_PREFERENCES_DIRECTORY, fileName)
}

/** The document's fields when the file exists, parses and carries `schema`; otherwise null. */
export function readPreferenceDocument(filePath: string, schema: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(readFileSync(filePath, "utf8"))
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null
    const record = parsed as Record<string, unknown>
    return record.schema === schema ? record : null
  } catch {
    return null
  }
}

export async function writePreferenceDocument(filePath: string, schema: string, fields: Record<string, unknown>): Promise<void> {
  await mkdir(path.dirname(filePath), { recursive: true })
  const temporary = `${filePath}.${randomUUID()}.tmp`
  try {
    await writeFile(temporary, `${JSON.stringify({ schema, ...fields })}\n`, { encoding: "utf8", flag: "wx" })
    await rename(temporary, filePath)
  } catch (error) {
    await rm(temporary, { force: true })
    throw error
  }
}

export async function removePreferenceDocument(filePath: string): Promise<void> {
  await rm(filePath, { force: true })
}

/**
 * Serializes writes to one document so rapid changes land on disk in the order
 * they were made; a failed write does not block the ones after it.
 */
export class PreferenceWriteQueue {
  private tail: Promise<void> = Promise.resolve()

  run(write: () => Promise<void>): Promise<void> {
    const next = this.tail.then(write)
    this.tail = next.catch(() => undefined)
    return next
  }
}
