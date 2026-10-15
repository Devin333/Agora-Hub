import { appendFile, mkdir, stat, rename, rm } from "node:fs/promises"
import path from "node:path"
import { redactRecord } from "./redaction"

const MAX_LOG_BYTES = 2 * 1024 * 1024

export class AuditLogger {
  readonly filePath: string

  constructor(logDirectory: string) {
    this.filePath = path.join(logDirectory, "desktop-host.jsonl")
  }

  async write(event: string, details: Record<string, unknown> = {}): Promise<void> {
    try {
      await mkdir(path.dirname(this.filePath), { recursive: true })
      await this.rotateIfNeeded()
      const line = JSON.stringify({ at: new Date().toISOString(), event, ...redactRecord(details) })
      await appendFile(this.filePath, `${line}\n`, { encoding: "utf8", mode: 0o600 })
    } catch {
      // Logging must never create a second failure path during shutdown or recovery.
    }
  }

  private async rotateIfNeeded(): Promise<void> {
    try {
      const current = await stat(this.filePath)
      if (current.size < MAX_LOG_BYTES) return
      await rm(`${this.filePath}.1`, { force: true })
      await rename(this.filePath, `${this.filePath}.1`)
    } catch {
      // Missing or locked logs are harmless; the next append remains bounded by the OS.
    }
  }
}
