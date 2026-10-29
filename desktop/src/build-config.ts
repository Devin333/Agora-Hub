import { readFileSync } from "node:fs"
import path from "node:path"
import { parseServerOrigin, type ServerOriginPolicy } from "./server-origin"

/**
 * What a build was made for, written by scripts/build.mjs to `out/build.json`:
 *
 * - `officialOrigin`: the official service (AGORA_DESKTOP_OFFICIAL_URL, or the
 *   older AGORA_DESKTOP_SERVER_URL). Without one the official choice is hidden.
 * - `imageTag`: the release of the server images the local stack runs,
 *   `v<app version>` for a release build (AGORA_DESKTOP_IMAGE_TAG overrides it).
 *
 * The host validates the file again on every launch, so a bad value only means
 * the corresponding feature is unavailable.
 */
export const BUILD_CONFIG_FILE_NAME = "build.json"
export const BUILD_CONFIG_SCHEMA = "agora.desktop-build/v1"

export interface DesktopBuildConfig {
  officialOrigin: string | null
  imageTag: string | null
}

/**
 * Development only: an unpackaged host reads AGORA_DESKTOP_IMAGE_TAG and
 * AGORA_DESKTOP_IMAGE_REGISTRY when it starts, so a developer (or the e2e
 * suite) can run the local stack from images built on this computer.
 */
export interface DevelopmentImageOverride {
  imageTag: string | null
  imageRegistry: string | null
}

export function developmentImageOverride(env: NodeJS.ProcessEnv): DevelopmentImageOverride {
  const tag = env.AGORA_DESKTOP_IMAGE_TAG?.trim()
  const registry = env.AGORA_DESKTOP_IMAGE_REGISTRY?.trim()
  return { imageTag: isImageTag(tag) ? tag : null, imageRegistry: isImageRegistry(registry) ? registry : null }
}

const IMAGE_TAG = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/
const IMAGE_REGISTRY = /^[a-z0-9]+(?:[._-][a-z0-9]+)*(?::\d+)?(?:\/[a-z0-9]+(?:[._-][a-z0-9]+)*)*$/

export function buildConfigFilePath(outputDirectory: string): string {
  return path.join(outputDirectory, BUILD_CONFIG_FILE_NAME)
}

export function isImageTag(value: unknown): value is string {
  return typeof value === "string" && IMAGE_TAG.test(value)
}

export function isImageRegistry(value: unknown): value is string {
  return typeof value === "string" && value.length <= 255 && IMAGE_REGISTRY.test(value)
}

export function readBuildConfig(filePath: string, policy: ServerOriginPolicy): DesktopBuildConfig {
  const empty: DesktopBuildConfig = { officialOrigin: null, imageTag: null }
  let document: unknown
  try {
    document = JSON.parse(readFileSync(filePath, "utf8"))
  } catch {
    return empty
  }
  if (!document || typeof document !== "object" || Array.isArray(document)) return empty
  const record = document as Record<string, unknown>
  if (record.schema !== BUILD_CONFIG_SCHEMA) return empty
  const parsed = record.officialOrigin === null ? null : parseServerOrigin(record.officialOrigin, policy)
  return {
    officialOrigin: parsed?.ok ? parsed.origin : null,
    imageTag: isImageTag(record.imageTag) ? record.imageTag : null
  }
}
