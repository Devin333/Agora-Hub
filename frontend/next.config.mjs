import { fileURLToPath } from "node:url"
import path from "node:path"
import { loadRootEnv } from "./root-env.mjs"

const configDir = path.dirname(fileURLToPath(import.meta.url))
loadRootEnv(configDir)

const deploymentProfile = process.env.NEWSROOM_DEPLOYMENT_PROFILE ?? "default"
if (!new Set(["default", "local-desktop"]).has(deploymentProfile)) throw new Error(`Unsupported NEWSROOM_DEPLOYMENT_PROFILE: ${deploymentProfile}`)
const canonicalOrigin = process.env.NEWSROOM_CANONICAL_ORIGIN
const legacyDesktopOrigin = process.env.NEWSROOM_DESKTOP_CANONICAL_ORIGIN
if (canonicalOrigin && legacyDesktopOrigin && canonicalOrigin.replace(/\/$/, "") !== legacyDesktopOrigin.replace(/\/$/, "")) throw new Error("NEWSROOM_CANONICAL_ORIGIN and NEWSROOM_DESKTOP_CANONICAL_ORIGIN must match")
const resolvedCanonicalOrigin = canonicalOrigin ?? legacyDesktopOrigin ?? "http://127.0.0.1:3000"
const parsedCanonicalOrigin = new URL(resolvedCanonicalOrigin)
if (parsedCanonicalOrigin.origin !== resolvedCanonicalOrigin.replace(/\/$/, "")) throw new Error("canonical origin must contain only scheme, host, and port")
if (deploymentProfile === "local-desktop") {
  if (!new Set(["127.0.0.1", "localhost", "[::1]"]).has(parsedCanonicalOrigin.hostname)) throw new Error("local-desktop canonical origin must use a loopback host")
  if (!["http:", "https:"].includes(parsedCanonicalOrigin.protocol)) throw new Error("local-desktop canonical origin must use HTTP or HTTPS")
  if (parsedCanonicalOrigin.username || parsedCanonicalOrigin.password || parsedCanonicalOrigin.pathname !== "/" || parsedCanonicalOrigin.search || parsedCanonicalOrigin.hash) throw new Error("local-desktop canonical origin must contain only scheme, loopback host, and port")
}
const apiBaseUrl = process.env.NEWSROOM_API_BASE_URL ?? process.env.NEXT_PUBLIC_NEWSROOM_API_BASE_URL ?? "http://127.0.0.1:8000"
const parsedApi = new URL(apiBaseUrl)
if (deploymentProfile === "local-desktop") {
  if (!new Set(["127.0.0.1", "localhost", "[::1]"]).has(parsedApi.hostname)) throw new Error("local-desktop API base must use a loopback host")
  if (!["http:", "https:"].includes(parsedApi.protocol)) throw new Error("local-desktop API base must use HTTP or HTTPS")
  if (parsedApi.username || parsedApi.password || parsedApi.pathname !== "/" || parsedApi.search || parsedApi.hash || parsedApi.origin !== apiBaseUrl.replace(/\/$/, "")) throw new Error("local-desktop API base must contain only scheme, loopback host, and port")
}

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  devIndicators: false,
  ...(deploymentProfile === "local-desktop"
    ? { experimental: { isrFlushToDisk: false } }
    : {}),
  env: { NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE: deploymentProfile },
  async rewrites() {
    if (deploymentProfile === "local-desktop") return []
    return [{ source: "/api/v1/:path*", destination: `${apiBaseUrl.replace(/\/$/, "")}/api/v1/:path*` }]
  }
}
export default nextConfig
