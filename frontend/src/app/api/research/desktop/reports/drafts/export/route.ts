import { NextRequest } from "next/server"
import { desktopPost } from "@/lib/research/desktop/server-proxy"
import { sanitizeDraftExport } from "@/lib/research/desktop/request-contracts"

export const dynamic = "force-dynamic"

export const POST = (request: NextRequest) =>
  desktopPost(
    "/api/v1/research/desktop/reports/drafts/export",
    request,
    sanitizeDraftExport,
  )
