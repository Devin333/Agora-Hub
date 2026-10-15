import { NextRequest } from "next/server"
import { desktopGet, personalMemoryPut } from "../_server"
import { sanitizePersonalMemoryPolicy } from "@/lib/research/personal-memory-contracts"

export const dynamic = "force-dynamic"
export const GET = (request: NextRequest) => desktopGet("/api/v1/research/desktop/memory/policy", request)
export const PUT = (request: NextRequest) =>
  personalMemoryPut("/api/v1/research/desktop/memory/policy", request, sanitizePersonalMemoryPolicy)
