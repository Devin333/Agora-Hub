import type { NextRequest } from "next/server"
import { sanitizePrepareRequest } from "@/lib/research/desktop/request-contracts"
import { desktopPost } from "@/lib/research/desktop/server-proxy"

export const dynamic = "force-dynamic"

export async function POST(request: NextRequest) {
  return desktopPost(
    "/api/v1/research/desktop/operations/prepare",
    request,
    sanitizePrepareRequest,
  )
}
