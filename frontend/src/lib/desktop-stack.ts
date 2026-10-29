import { createHash, timingSafeEqual } from "node:crypto"

// The desktop app runs a stack of its own on this computer and must not mistake
// another program listening on the same loopback port for it. It generates a
// random token, hands it to the stack it starts, and before opening the stack
// asks it to prove it holds the same token and which release it runs.
export const DESKTOP_STACK_TOKEN_HEADER = "x-agora-desktop-stack-token"
export const DESKTOP_STACK_TOKEN_ENV = "AGORA_DESKTOP_STACK_TOKEN"
export const DESKTOP_STACK_IMAGE_TAG_ENV = "AGORA_IMAGE_TAG"

// Shorter tokens are refused, so a misconfigured stack cannot be guessed.
const MIN_TOKEN_LENGTH = 32

export type DesktopStackIdentity = { ok: true; imageTag: string }

// The only variables these checks read, so callers can pass any environment.
type DesktopStackEnv = Readonly<Record<string, string | undefined>>

export function desktopStackToken(env: DesktopStackEnv = process.env): string | null {
  const token = String(env[DESKTOP_STACK_TOKEN_ENV] ?? "").trim()
  return token.length >= MIN_TOKEN_LENGTH ? token : null
}

// Returns the identity only to a caller presenting this stack's token. A server
// that is not a desktop-managed stack has no token and never answers.
export function desktopStackIdentity(presented: string | null, env: DesktopStackEnv = process.env): DesktopStackIdentity | null {
  const expected = desktopStackToken(env)
  if (!expected || !presented || !tokensMatch(presented, expected)) return null
  return { ok: true, imageTag: String(env[DESKTOP_STACK_IMAGE_TAG_ENV] ?? "").trim() || "dev" }
}

// Hashing first gives both sides the same length, so the comparison time does
// not reveal the token's length or any matching prefix.
function tokensMatch(presented: string, expected: string) {
  return timingSafeEqual(digest(presented), digest(expected))
}

function digest(value: string) {
  return createHash("sha256").update(value, "utf8").digest()
}
