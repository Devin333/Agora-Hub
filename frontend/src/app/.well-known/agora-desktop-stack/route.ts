import { NextRequest, NextResponse } from "next/server"
import { DESKTOP_STACK_TOKEN_HEADER, desktopStackIdentity } from "@/lib/desktop-stack"

export const dynamic = "force-dynamic"
export const runtime = "nodejs"

const NO_STORE = { "Cache-Control": "no-store" }

export function GET(request: NextRequest) {
  const identity = desktopStackIdentity(request.headers.get(DESKTOP_STACK_TOKEN_HEADER))
  // Indistinguishable from a route that does not exist, for any other caller.
  if (!identity) return new NextResponse(null, { status: 404, headers: NO_STORE })
  return NextResponse.json(identity, { headers: NO_STORE })
}
