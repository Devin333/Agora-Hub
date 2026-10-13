import { NextRequest } from "next/server"
import { desktopGet, desktopPost } from "@/lib/research/desktop/server-proxy"
import { sanitizeSavedReport } from "@/lib/research/desktop/request-contracts"

export const dynamic = "force-dynamic"

export const GET = (request: NextRequest) =>
  desktopGet("/api/v1/research/desktop/reports", request)

export const POST = (request: NextRequest) =>
  desktopPost("/api/v1/research/desktop/reports", request, sanitizeSavedReport)
