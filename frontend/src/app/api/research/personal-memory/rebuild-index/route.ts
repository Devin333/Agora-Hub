import { NextRequest, NextResponse } from "next/server"
import { desktopPost } from "../_server"
import { sanitizePersonalMemoryIndexRebuild } from "@/lib/research/personal-memory-contracts"

export const dynamic = "force-dynamic"

const PRIVATE_HEADERS = {
  "Cache-Control": "private, no-store",
  "Pragma": "no-cache",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
}

export async function POST(request: NextRequest) {
  const upstream = await desktopPost(
    "/api/v1/research/desktop/memory/rebuild-index",
    request,
    sanitizePersonalMemoryIndexRebuild,
  )
  const payload = await readJsonObject(upstream)

  if (!upstream.ok) {
    const error = asObject(payload?.error)
    return NextResponse.json(
      {
        success: false,
        error: {
          code: safeFailureCode(error?.code),
          message: "个人记忆索引重建失败。",
        },
      },
      { status: upstream.status >= 400 ? upstream.status : 503, headers: PRIVATE_HEADERS },
    )
  }

  const data = asObject(payload?.data)
  const indexedRecords = data?.indexed_records
  if (payload?.success !== true || typeof indexedRecords !== "number" || !Number.isSafeInteger(indexedRecords) || indexedRecords < 0) {
    return NextResponse.json(
      {
        success: false,
        error: {
          code: "invalid_upstream_response",
          message: "个人记忆服务返回了无效的索引重建结果。",
        },
      },
      { status: 502, headers: PRIVATE_HEADERS },
    )
  }

  return NextResponse.json(
    { success: true, data: { indexed_records: indexedRecords } },
    { headers: PRIVATE_HEADERS },
  )
}

async function readJsonObject(response: Response): Promise<Record<string, unknown> | null> {
  try {
    return asObject(await response.json())
  } catch {
    return null
  }
}

function asObject(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

const PUBLIC_FAILURE_CODES = new Set([
  "auth_required",
  "auth_invalid_origin",
  "invalid_request",
  "not_found",
  "research_service_unavailable",
  "personal_memory_state_invalid",
  "personal_memory_store_error",
  "personal_memory_store_invalid",
  "personal_memory_not_found",
  "personal_memory_conflict",
  "personal_memory_store_unavailable",
  "personal_memory_store_corrupt",
])

function safeFailureCode(value: unknown): string {
  return typeof value === "string" && PUBLIC_FAILURE_CODES.has(value)
    ? value
    : "research_service_unavailable"
}
