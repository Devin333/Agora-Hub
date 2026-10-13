import { resolveDeploymentProfile } from "@/lib/deployment-profile"

export type DesktopRuntimeIdentity = {
  deployment_profile: "local-desktop"
  instance_key: string
  release_id: string
  config_digest: string
  boot_id: string
}

export function frontendRuntimeIdentity(
  env: NodeJS.ProcessEnv = process.env,
): DesktopRuntimeIdentity | null {
  if (resolveDeploymentProfile(env.NEWSROOM_DEPLOYMENT_PROFILE ?? "default") !== "local-desktop") return null
  const instanceKey = env.NEWSROOM_DESKTOP_INSTANCE_KEY?.trim() ?? ""
  const releaseId = env.NEWSROOM_DESKTOP_RELEASE_ID?.trim() ?? ""
  const configDigest = env.NEWSROOM_DESKTOP_CONFIG_DIGEST?.trim() ?? ""
  const bootId = env.NEWSROOM_DESKTOP_BOOT_ID?.trim() ?? ""
  if (
    !/^local-desktop:https?:\/\/(?:127\.0\.0\.1|localhost|\[::1\])(?::[0-9]{1,5})?$/.test(instanceKey) ||
    !/^[A-Za-z0-9._:-]{1,200}$/.test(releaseId) ||
    !/^[0-9a-f]{64}$/.test(configDigest) ||
    !/^[0-9a-f]{32}$/.test(bootId)
  ) {
    throw new Error("local-desktop frontend runtime identity is incomplete or invalid")
  }
  return {
    deployment_profile: "local-desktop",
    instance_key: instanceKey,
    release_id: releaseId,
    config_digest: configDigest,
    boot_id: bootId,
  }
}

export function attachFrontendRuntimeIdentity<T extends Record<string, unknown>>(
  data: T,
  env: NodeJS.ProcessEnv = process.env,
): T | (T & { local_desktop: DesktopRuntimeIdentity }) {
  const identity = frontendRuntimeIdentity(env)
  return identity ? { ...data, local_desktop: identity } : data
}
