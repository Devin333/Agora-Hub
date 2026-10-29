import { fileURLToPath } from "node:url"
import path from "node:path"
import { loadRootEnv } from "./root-env.mjs"
import { resolveApiBaseUrl, resolveDeploymentProfile, resolvePublicOrigin } from "./deployment-config.mjs"

const configDir = path.dirname(fileURLToPath(import.meta.url))
loadRootEnv(configDir)

const deploymentProfile = resolveDeploymentProfile(process.env.NEWSROOM_DEPLOYMENT_PROFILE)
const hosted = deploymentProfile === "hosted"
const apiBaseUrl = resolveApiBaseUrl(process.env)
// A hosted server refuses to start without a valid public origin; a build may
// run before the origin is known, so only validate it when one is supplied.
if (hosted && process.env.NEWSROOM_PUBLIC_ORIGIN) resolvePublicOrigin(process.env)

// `next dev` only trusts its own `localhost` host for dev resources (HMR, dev chunks);
// the app is opened on a loopback IP, so allow the loopback hosts and nothing else.
const allowedDevOrigins = ["127.0.0.1", "::1", "localhost"]

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  devIndicators: false,
  poweredByHeader: false,
  allowedDevOrigins,
  ...(hosted ? { output: "standalone", outputFileTracingRoot: configDir } : {}),
  env: { NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE: deploymentProfile },
  async rewrites() {
    // Hosted requests reach the API only through the audited BFF routes.
    if (hosted) return []
    return [{ source: "/api/v1/:path*", destination: `${apiBaseUrl}/api/v1/:path*` }]
  },
}
export default nextConfig
