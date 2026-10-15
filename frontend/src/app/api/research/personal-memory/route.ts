import { NextRequest } from "next/server"
import { desktopFailure, desktopGet, desktopPost } from "./_server"
import { personalMemoryListPath, sanitizePersonalMemoryCreate } from "@/lib/research/personal-memory-contracts"

export const dynamic = "force-dynamic"
export function GET(request: NextRequest) {
  const path = personalMemoryListPath(request.nextUrl.searchParams)
  return path ? desktopGet(path, request) : desktopFailure("invalid_request", 400)
}
export const POST = (request: NextRequest) =>
  desktopPost("/api/v1/research/desktop/memory", request, sanitizePersonalMemoryCreate)
