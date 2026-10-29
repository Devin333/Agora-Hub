import { randomBytes } from "node:crypto"

/**
 * The `.env` the local stack's compose file reads.
 *
 * The packaged `env.template` (deploy/desktop) is the single list of keys the
 * compose file needs; this module fills it in. Secrets are generated once and
 * kept across every later write: replacing them would make the model keys and
 * run records already stored in the stack unreadable.
 */
export const STACK_SECRET_KEYS = ["NEWSROOM_CREDENTIAL_ENCRYPTION_KEY", "NEWS_ACTIVITY_ENCRYPTION_KEY", "AGORA_DESKTOP_STACK_TOKEN"] as const
export const STACK_SETTING_KEYS = ["AGORA_WEB_PORT", "AGORA_IMAGE_REGISTRY", "AGORA_IMAGE_TAG"] as const
export type StackEnvKey = (typeof STACK_SECRET_KEYS)[number] | (typeof STACK_SETTING_KEYS)[number]
export type StackEnv = Record<StackEnvKey, string>

const KEY_LINE = /^([A-Z][A-Z0-9_]*)=(.*)$/

export type RandomBytes = (size: number) => Buffer

/**
 * Fresh secrets in the encodings the server checks: the credential key is
 * standard base64, the activity key a Fernet key (URL-safe base64 with its
 * padding, which Python's decoder requires), the stack token URL-safe text.
 */
export function generateStackSecrets(random: RandomBytes = randomBytes): Pick<StackEnv, (typeof STACK_SECRET_KEYS)[number]> {
  return {
    NEWSROOM_CREDENTIAL_ENCRYPTION_KEY: random(32).toString("base64"),
    NEWS_ACTIVITY_ENCRYPTION_KEY: random(32).toString("base64").replace(/\+/g, "-").replace(/\//g, "_"),
    AGORA_DESKTOP_STACK_TOKEN: random(32).toString("base64url")
  }
}

export function parseEnvFile(text: string): Map<string, string> {
  const values = new Map<string, string>()
  for (const line of text.split(/\r?\n/)) {
    const match = KEY_LINE.exec(line.trim())
    if (match) values.set(match[1], match[2])
  }
  return values
}

/** The keys the template declares, in order. */
export function templateKeys(template: string): string[] {
  return [...parseEnvFile(template).keys()]
}

/**
 * The template with every key filled in. A template key this module does not
 * know, or one it knows that the template lacks, means the packaged files and
 * the app disagree; that is refused rather than written half-filled.
 */
export function renderEnvFile(template: string, values: StackEnv): string {
  const declared = new Set(templateKeys(template))
  const known = new Set<string>([...STACK_SECRET_KEYS, ...STACK_SETTING_KEYS])
  for (const key of declared) if (!known.has(key)) throw new Error(`stack template declares unknown key ${key}`)
  for (const key of known) if (!declared.has(key)) throw new Error(`stack template lacks key ${key}`)
  for (const [key, value] of Object.entries(values)) {
    if (!value || /[\r\n\s"'$#`\\]/.test(value)) throw new Error(`stack value for ${key} is empty or unsafe`)
  }
  const lines = template.split(/\r?\n/).map(line => {
    const match = KEY_LINE.exec(line.trim())
    return match ? `${match[1]}=${values[match[1] as StackEnvKey]}` : line
  })
  return lines.join("\n").replace(/\n*$/, "\n")
}

/**
 * The values to write: secrets and the chosen port from the existing file when
 * it has them, everything else from `settings`.
 */
export function mergeStackEnv(
  existing: Map<string, string>,
  settings: Pick<StackEnv, "AGORA_IMAGE_REGISTRY" | "AGORA_IMAGE_TAG"> & { AGORA_WEB_PORT?: string },
  random: RandomBytes = randomBytes
): StackEnv {
  const generated = generateStackSecrets(random)
  const secrets = Object.fromEntries(STACK_SECRET_KEYS.map(key => [key, existing.get(key) || generated[key]])) as Pick<StackEnv, (typeof STACK_SECRET_KEYS)[number]>
  const port = settings.AGORA_WEB_PORT ?? existing.get("AGORA_WEB_PORT") ?? ""
  return { ...secrets, AGORA_WEB_PORT: port, AGORA_IMAGE_REGISTRY: settings.AGORA_IMAGE_REGISTRY, AGORA_IMAGE_TAG: settings.AGORA_IMAGE_TAG }
}

export function parsePort(value: string | undefined): number | null {
  if (!value || !/^\d{1,5}$/.test(value)) return null
  const port = Number(value)
  return port >= 1024 && port <= 65535 ? port : null
}
