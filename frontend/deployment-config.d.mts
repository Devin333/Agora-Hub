// Only reads the NEWSROOM_* keys, so any string record (including process.env) works.
export type DeploymentEnv = Readonly<Record<string, string | undefined>>
export type DeploymentProfile = "default" | "hosted"
export const DEPLOYMENT_PROFILES: readonly DeploymentProfile[]
export function resolveDeploymentProfile(value?: string | null): DeploymentProfile
export function insecureOriginAllowed(env?: DeploymentEnv): boolean
export function resolvePublicOrigin(env?: DeploymentEnv): string
export function resolveApiBaseUrl(env?: DeploymentEnv): string
