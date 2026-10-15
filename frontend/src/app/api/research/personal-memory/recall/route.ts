import { NextRequest } from "next/server"
import { desktopPost } from "../_server"
import { sanitizePersonalMemoryRecall } from "@/lib/research/personal-memory-contracts"

export const dynamic = "force-dynamic"
export const POST = (request: NextRequest) =>
  desktopPost("/api/v1/research/desktop/memory/recall", request, sanitizePersonalMemoryRecall)
