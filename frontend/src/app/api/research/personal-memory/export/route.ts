import { NextRequest } from "next/server"
import { desktopFailure, desktopGet } from "../_server"
import { personalMemoryExportPath } from "@/lib/research/personal-memory-contracts"

export const dynamic = "force-dynamic"
export function GET(request: NextRequest) {
  const path = personalMemoryExportPath(request.nextUrl.searchParams)
  return path ? desktopGet(path, request) : desktopFailure("invalid_request", 400)
}
