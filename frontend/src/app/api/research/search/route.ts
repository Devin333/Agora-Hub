import { NextRequest } from "next/server"
import { guidedResearchProxy } from "@/lib/research/guided-proxy"

export const dynamic = "force-dynamic"
export const POST = (request: NextRequest) => guidedResearchProxy(request, "search")
