import { isIP } from "node:net"
import { headers } from "next/headers"
import { isHostedProfile } from "@/lib/deployment-profile"

// Server-side only. The API keys login throttling and session audit on the
// browser's address, but it only ever sees the web server as its peer. The
// hosted web server is reachable only through the TLS proxy, which replaces any
// client-supplied X-Forwarded-For with the address it accepted the connection
// from; Next adds the socket peer when the header is absent. The BFF passes
// that chain on, and the API reads it only because the BFF's own address is
// inside NEWSROOM_TRUSTED_PROXY_CIDRS.
export const FORWARDED_FOR_HEADER = "X-Forwarded-For"
const MAX_HOPS = 8
const MAX_LENGTH = 512

/** The forwarded-address chain to send upstream, or none when it is absent or malformed. */
export function forwardedClientHeaders(source: Pick<Headers, "get">): Record<string, string> {
  if (!isHostedProfile()) return {}
  const chain = forwardedChain(source.get("x-forwarded-for"))
  return chain ? { [FORWARDED_FOR_HEADER]: chain } : {}
}

/** The same headers for code that runs inside a request but holds no request object. */
export async function currentForwardedClientHeaders(): Promise<Record<string, string>> {
  if (!isHostedProfile()) return {}
  return forwardedClientHeaders(await headers())
}

export function forwardedChain(value: string | null): string | null {
  if (!value || value.length > MAX_LENGTH) return null
  const hops = value.split(",").map(hop => hop.trim())
  if (hops.length > MAX_HOPS || hops.some(hop => isIP(hop) === 0)) return null
  return hops.join(", ")
}
