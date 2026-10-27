import {
  resolveApiBaseUrl,
  resolveDeploymentProfile as resolveConfiguredProfile,
  resolvePublicOrigin,
  type DeploymentProfile,
} from "../../deployment-config.mjs"

export type { DeploymentProfile }

function configuredProfile() {
  return process.env.NEWSROOM_DEPLOYMENT_PROFILE ?? process.env.NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE
}

export function resolveDeploymentProfile(value = configuredProfile()): DeploymentProfile {
  return resolveConfiguredProfile(value)
}

export function isHostedProfile(value = configuredProfile()) {
  return resolveDeploymentProfile(value) === "hosted"
}

// Server-side only: the browser-facing origin every hosted mutation must carry.
export function hostedPublicOrigin(): string {
  return resolvePublicOrigin(process.env)
}

// Server-side only: where the BFF reaches the API, normally a private address.
export function apiBaseUrl(): string {
  return resolveApiBaseUrl(process.env)
}

export function hostedAllowsInsecureCookie() {
  return isHostedProfile() && new URL(hostedPublicOrigin()).protocol === "http:"
}

// The API checks Origin on every hosted mutation; the BFF has already checked
// the browser's own Origin, so it forwards the configured public origin.
export function serverOwnedMutationOriginHeaders(): Record<string, string> {
  return isHostedProfile() ? { Origin: hostedPublicOrigin() } : {}
}
