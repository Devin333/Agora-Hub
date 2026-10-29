import { hostPreferencePath, PreferenceWriteQueue, readPreferenceDocument, writePreferenceDocument } from "./host-preferences"
import type { ConnectionMode } from "./contracts"
import { parseServerOrigin, type ServerOriginPolicy } from "./server-origin"

/**
 * Which connection this client opens: the build's official service, a server
 * by its address, or the stack the app runs on this computer.
 *
 * The user's saved choice wins; without one, a build with an official service
 * opens it and any other build asks. A stored address is re-validated on
 * every read, so a hand-edited or older file can never point the window at
 * something the policy would refuse; a saved "official" choice in a build
 * without an official service counts as no choice. Files written by earlier
 * versions (v1: just an address) are read as the equivalent choice and
 * rewritten in the current format on the next save.
 */
export interface ConnectionChoiceView {
  mode: ConnectionMode | null
  /** The last server address the user saved, kept while another mode is chosen. */
  serverOrigin: string | null
  officialOrigin: string | null
}

export const CONNECTION_FILE_NAME = "connection.json"
export const CONNECTION_SCHEMA = "agora.desktop-connection/v2"
const LEGACY_CONNECTION_SCHEMA = "agora.desktop-connection/v1"
const MODES: readonly ConnectionMode[] = ["official", "server", "local"]

export function connectionFilePath(userDataPath: string): string {
  return hostPreferencePath(userDataPath, CONNECTION_FILE_NAME)
}

interface StoredChoice {
  mode: ConnectionMode | null
  serverOrigin: string | null
}

function validOrigin(value: unknown, policy: ServerOriginPolicy): string | null {
  if (value === null || value === undefined) return null
  const parsed = parseServerOrigin(value, policy)
  return parsed.ok ? parsed.origin : null
}

function readStoredChoice(filePath: string, policy: ServerOriginPolicy, officialOrigin: string | null): StoredChoice | null {
  const current = readPreferenceDocument(filePath, CONNECTION_SCHEMA)
  if (current) {
    const mode = MODES.includes(current.mode as ConnectionMode) ? current.mode as ConnectionMode : null
    return { mode, serverOrigin: validOrigin(current.serverOrigin, policy) }
  }
  const legacy = readPreferenceDocument(filePath, LEGACY_CONNECTION_SCHEMA)
  if (!legacy) return null
  const origin = validOrigin(legacy.origin, policy)
  if (!origin) return null
  return origin === officialOrigin ? { mode: "official", serverOrigin: null } : { mode: "server", serverOrigin: origin }
}

export interface ConnectionStoreOptions {
  connectionFile: string
  officialOrigin: string | null
  policy: ServerOriginPolicy
}

export class ServerConnectionStore {
  private stored: StoredChoice | null
  private readonly writes = new PreferenceWriteQueue()

  constructor(private readonly options: ConnectionStoreOptions) {
    this.stored = readStoredChoice(options.connectionFile, options.policy, options.officialOrigin)
  }

  get policy(): ServerOriginPolicy {
    return this.options.policy
  }

  get view(): ConnectionChoiceView {
    const officialOrigin = this.options.officialOrigin
    const serverOrigin = this.stored?.serverOrigin ?? null
    return { mode: this.resolvedMode(), serverOrigin, officialOrigin }
  }

  async saveOfficial(): Promise<ConnectionChoiceView> {
    if (!this.options.officialOrigin) throw new Error("official_service_unavailable")
    return await this.write({ mode: "official", serverOrigin: this.stored?.serverOrigin ?? null })
  }

  /** Persists an origin already validated by {@link parseServerOrigin}. */
  async saveServer(origin: string): Promise<ConnectionChoiceView> {
    const parsed = parseServerOrigin(origin, this.options.policy)
    if (!parsed.ok) throw new Error(parsed.code)
    return await this.write({ mode: "server", serverOrigin: parsed.origin })
  }

  async saveLocal(): Promise<ConnectionChoiceView> {
    return await this.write({ mode: "local", serverOrigin: this.stored?.serverOrigin ?? null })
  }

  private resolvedMode(): ConnectionMode | null {
    const mode = this.stored?.mode ?? null
    if (mode === "official") return this.options.officialOrigin ? "official" : null
    if (mode === "server") return this.stored?.serverOrigin ? "server" : null
    if (mode === "local") return "local"
    return this.options.officialOrigin ? "official" : null
  }

  /** The new choice takes effect only once it is on disk; a failed write leaves the previous one in place. */
  private async write(choice: StoredChoice): Promise<ConnectionChoiceView> {
    await this.writes.run(() => writePreferenceDocument(this.options.connectionFile, CONNECTION_SCHEMA, { mode: choice.mode, serverOrigin: choice.serverOrigin }))
    this.stored = choice
    return this.view
  }
}
