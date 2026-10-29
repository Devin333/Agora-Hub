import { readFileSync } from "node:fs"
import path from "node:path"
import { hostPreferencePath, PreferenceWriteQueue, readPreferenceDocument, removePreferenceDocument, writePreferenceDocument } from "./host-preferences"
import { parseServerOrigin, type ServerOriginPolicy } from "./server-origin"

/**
 * Which server this client opens.
 *
 * The address a user saved on this computer wins; otherwise the address the
 * build was made for (`out/server.json`, written by scripts/build.mjs from
 * AGORA_DESKTOP_SERVER_URL) is used; with neither the client asks for one.
 * A stored address is re-validated on every read, so a hand-edited or older
 * file can never point the window at something the policy would refuse.
 */
export type ServerOriginSource = "user" | "bundled"

export interface ServerConnectionView {
  origin: string | null
  source: ServerOriginSource | null
  /** The build's own address, offered back when the user resets their choice. */
  bundledOrigin: string | null
}

export const CONNECTION_FILE_NAME = "connection.json"
export const BUNDLED_SERVER_FILE_NAME = "server.json"
const CONNECTION_SCHEMA = "agora.desktop-connection/v1"
export const BUNDLED_SERVER_SCHEMA = "agora.desktop-server/v1"

export function connectionFilePath(userDataPath: string): string {
  return hostPreferencePath(userDataPath, CONNECTION_FILE_NAME)
}

export function bundledServerFilePath(outputDirectory: string): string {
  return path.join(outputDirectory, BUNDLED_SERVER_FILE_NAME)
}

export function readBundledServerOrigin(filePath: string, policy: ServerOriginPolicy): string | null {
  let document: unknown
  try {
    document = JSON.parse(readFileSync(filePath, "utf8"))
  } catch {
    return null
  }
  if (!document || typeof document !== "object" || Array.isArray(document)) return null
  const record = document as Record<string, unknown>
  if (record.schema !== BUNDLED_SERVER_SCHEMA || record.origin === null) return null
  const parsed = parseServerOrigin(record.origin, policy)
  return parsed.ok ? parsed.origin : null
}

function readUserOrigin(filePath: string, policy: ServerOriginPolicy): string | null {
  const document = readPreferenceDocument(filePath, CONNECTION_SCHEMA)
  if (!document) return null
  const parsed = parseServerOrigin(document.origin, policy)
  return parsed.ok ? parsed.origin : null
}

export interface ServerConnectionOptions {
  connectionFile: string
  bundledOrigin: string | null
  policy: ServerOriginPolicy
}

export class ServerConnectionStore {
  private userOrigin: string | null
  private readonly writes = new PreferenceWriteQueue()

  constructor(private readonly options: ServerConnectionOptions) {
    this.userOrigin = readUserOrigin(options.connectionFile, options.policy)
  }

  get policy(): ServerOriginPolicy {
    return this.options.policy
  }

  get view(): ServerConnectionView {
    const bundledOrigin = this.options.bundledOrigin
    if (this.userOrigin) return { origin: this.userOrigin, source: "user", bundledOrigin }
    if (bundledOrigin) return { origin: bundledOrigin, source: "bundled", bundledOrigin }
    return { origin: null, source: null, bundledOrigin }
  }

  /** Persists an origin already validated by {@link parseServerOrigin}. */
  async save(origin: string): Promise<ServerConnectionView> {
    const parsed = parseServerOrigin(origin, this.options.policy)
    if (!parsed.ok) throw new Error(parsed.code)
    // Choosing the build's own address is the same as having no choice of one's own.
    if (parsed.origin === this.options.bundledOrigin) return await this.reset()
    await this.writes.run(() => writePreferenceDocument(this.options.connectionFile, CONNECTION_SCHEMA, { origin: parsed.origin }))
    this.userOrigin = parsed.origin
    return this.view
  }

  async reset(): Promise<ServerConnectionView> {
    await this.writes.run(() => removePreferenceDocument(this.options.connectionFile))
    this.userOrigin = null
    return this.view
  }
}
