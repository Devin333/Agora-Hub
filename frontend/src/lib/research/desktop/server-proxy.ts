import { cookies } from "next/headers";
import { NextRequest, NextResponse } from "next/server";
import { NEWSROOM_SESSION_COOKIE } from "@/lib/auth/session";
import { authFailure, isSameOrigin } from "@/lib/auth/portal-server";
import { apiBaseUrl, serverOwnedMutationOriginHeaders } from "@/lib/deployment-profile";
const PRIVATE_HEADERS = { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff" };
const MAX_JSON_BYTES = 256 * 1024;
export async function desktopGet(path: string, request?: NextRequest) { return desktopForward(path, { method: "GET", request }); }
export async function desktopPost(path: string, request: NextRequest, sanitize: (value: Record<string, unknown>) => Record<string, unknown> | null) {
    if (!isSameOrigin(request))
        return authFailure("auth_invalid_origin", 403);
    const body = await readBoundedJson(request, MAX_JSON_BYTES);
    if (!body)
        return desktopFailure("invalid_request", 400);
    const sanitized = sanitize(body);
    if (!sanitized)
        return desktopFailure("invalid_request", 400);
    return desktopForward(path, { method: "POST", request, body: JSON.stringify(sanitized) });
}
export async function desktopBinaryPost(path: string, request: NextRequest, body: ArrayBuffer, headers: Record<string, string>) {
    if (!isSameOrigin(request))
        return authFailure("auth_invalid_origin", 403);
    return desktopForward(path, { method: "POST", request, body, headers });
}
async function desktopForward(path: string, options: {
    method: "GET" | "POST";
    request?: NextRequest;
    body?: BodyInit;
    headers?: Record<string, string>;
}) {
    const token = (await cookies()).get(NEWSROOM_SESSION_COOKIE)?.value;
    if (!token)
        return authFailure("auth_required", 401);
    const response = await fetch(`${apiBaseUrl()}${path}`, { method: options.method, headers: { Accept: "application/json", "X-Newsroom-Session": token, ...(options.method === "POST" ? serverOwnedMutationOriginHeaders() : {}), ...(typeof options.body === "string" ? { "Content-Type": "application/json" } : {}), ...options.headers }, body: options.body, cache: "no-store", signal: options.request ? AbortSignal.any([options.request.signal, AbortSignal.timeout(120000)]) : AbortSignal.timeout(120000) }).catch(() => null);
    if (!response)
        return desktopFailure("research_service_unavailable", 503, "研究服务暂时不可用，请稍后核对已有请求。");
    return new NextResponse(response.body, { status: response.status, headers: { ...PRIVATE_HEADERS, "Content-Type": response.headers.get("content-type") ?? "application/json", ...(response.headers.get("content-disposition") ? { "Content-Disposition": response.headers.get("content-disposition")! } : {}) } });
}
async function readBoundedJson(request: NextRequest, maximum: number): Promise<Record<string, unknown> | null> {
    const declared = Number(request.headers.get("content-length") ?? "0");
    if (Number.isFinite(declared) && declared > maximum)
        return null;
    const reader = request.body?.getReader();
    if (!reader)
        return null;
    const chunks: Uint8Array[] = [];
    let size = 0;
    try {
        while (true) {
            const chunk = await reader.read();
            if (chunk.done)
                break;
            size += chunk.value.byteLength;
            if (size > maximum) {
                await reader.cancel();
                return null;
            }
            ;
            chunks.push(chunk.value);
        }
    }
    catch {
        return null;
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
        bytes.set(chunk, offset);
        offset += chunk.byteLength;
    }
    try {
        const value = JSON.parse(new TextDecoder().decode(bytes));
        return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    }
    catch {
        return null;
    }
}
export function desktopFailure(code: string, status: number, message = "请求无效。") { return NextResponse.json({ success: false, error: { code, message } }, { status, headers: PRIVATE_HEADERS }); }
export function safeDesktopId(value: string) { return /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(value); }
export function rejectScopeFields(body: Record<string, unknown>) { return !["userId", "user_id", "tenantId", "tenant_id", "memoryNamespace", "memory_namespace"].some(key => key in body); }
