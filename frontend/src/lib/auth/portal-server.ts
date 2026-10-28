import { randomBytes } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";
import { safeApiPost, type SafeApiResult } from "@/lib/api/server";
import { NEWSROOM_SESSION_COOKIE, sessionCookieOptions } from "@/lib/auth/session";
import { hostedPublicOrigin, isHostedProfile, serverOwnedMutationOriginHeaders } from "@/lib/deployment-profile";
export const AUTH_BINDING_COOKIE = "newsroom_auth_binding";
export const AUTH_RETURN_COOKIE = "newsroom_auth_return";
export const AUTH_CALLBACK_COOKIE = "newsroom_auth_callback";
const privateHeaders = { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" };
// A function, so the public origin is read per request and not when the module loads during the build.
export function bindingOptions() {
    return { httpOnly: true, secure: sessionCookieOptions().secure, sameSite: "lax" as const, path: "/", maxAge: 900 };
}
export function bindingFor(request: NextRequest) {
    const existing = request.cookies.get(AUTH_BINDING_COOKIE)?.value;
    return existing && /^[A-Za-z0-9_-]{43}$/.test(existing) ? existing : randomBytes(32).toString("base64url");
}
export function safeReturnPath(value: unknown): string {
    if (typeof value !== "string" || !value.startsWith("/") || /[\\\x00-\x20]/.test(value) || value.startsWith("//"))
        return isHostedProfile() ? "/" : "/design-demo";
    try {
        const base = "https://agora.invalid";
        const parsed = new URL(value, base);
        const decoded = decodeURIComponent(parsed.pathname);
        if (parsed.origin !== base || /[\\\x00-\x20]/.test(decoded) || decoded.startsWith("//") || /^\/(?:api|login)(?:\/|$)/.test(decoded))
            return isHostedProfile() ? "/" : "/design-demo";
        return `${parsed.pathname}${parsed.search}${parsed.hash}`;
    }
    catch {
        return isHostedProfile() ? "/" : "/design-demo";
    }
}
export function authFailure(code: string, status = 400) {
    return NextResponse.json({ success: false, error: { code } }, { status, headers: privateHeaders });
}
export function requestOrigin(request: NextRequest) {
    if (isHostedProfile()) {
        // The reverse proxy forwards the public Host; any other Host means the
        // request did not arrive through the configured public origin.
        const origin = hostedPublicOrigin();
        return request.headers.get("host") === new URL(origin).host ? origin : null;
    }
    // Next's internal URL can use localhost even when the browser requests 127.0.0.1.
    // Host is supplied by the browser/proxy and cannot be set by cross-origin JS.
    const host = request.headers.get("host") ?? request.nextUrl.host;
    const protocol = request.nextUrl.protocol;
    try {
        const origin = new URL(`${protocol}//${host}`);
        return origin.host === host && !origin.username && !origin.password ? origin.origin : null;
    }
    catch {
        return null;
    }
}
export function isSameOrigin(request: NextRequest) {
    return request.headers.get("origin") === requestOrigin(request) && request.headers.get("sec-fetch-site") !== "cross-site";
}
export function authResponse(result: SafeApiResult<unknown>, { issueSession = true } = {}) {
    if (!result.ok) {
        const code = result.errorCode.startsWith("auth_") ? result.errorCode : "auth_service_unavailable";
        const status = code === "auth_rate_limited" ? 429 : /unavailable|delivery_failed/.test(code) ? 503 : 400;
        const response = authFailure(code, status);
        if (status === 429 && result.retryAfter)
            response.headers.set("Retry-After", String(result.retryAfter));
        return response;
    }
    const data = result.data as {
        session?: {
            sessionToken?: string;
            expiresAt?: string;
        };
    };
    if (data?.session) {
        const { sessionToken, ...session } = data.session;
        if (issueSession && !sessionToken)
            return authFailure("auth_service_unavailable", 502);
        const response = NextResponse.json({ success: true, data: { ...data, session } }, { headers: privateHeaders });
        if (issueSession && sessionToken)
            response.cookies.set(NEWSROOM_SESSION_COOKIE, sessionToken, sessionCookieOptions());
        return response;
    }
    return NextResponse.json({ success: true, data: result.data }, { headers: privateHeaders });
}
export async function publicAuthPost<T>(path: string, body?: unknown, init?: Omit<RequestInit, "headers"> & { headers?: Record<string, string> }): Promise<SafeApiResult<T>> {
    if (!/^\/api\/v1\/auth\//.test(path))
        return { ok: false, errorCode: "auth_service_unavailable", errorMessage: "Auth endpoint is not allowed" };
    const { headers: suppliedHeaders, ...requestInit } = init ?? {};
    const headers = { ...suppliedHeaders, ...serverOwnedMutationOriginHeaders() };
    return safeApiPost<T>(path, body, {
        ...requestInit,
        ...(Object.keys(headers).length > 0 ? { headers } : {}),
    });
}
export async function readBoundedJson(request: NextRequest, maximum = 16 * 1024): Promise<Record<string, unknown> | null> {
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
            chunks.push(chunk.value);
        }
        const bytes = new Uint8Array(size);
        let offset = 0;
        for (const chunk of chunks) {
            bytes.set(chunk, offset);
            offset += chunk.byteLength;
        }
        const value = JSON.parse(new TextDecoder().decode(bytes));
        return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : null;
    }
    catch {
        return null;
    }
}
export async function proxyAuthPost(request: NextRequest, path: string, parse: (body: Record<string, unknown>) => Record<string, unknown> | null) {
    if (!isSameOrigin(request))
        return authFailure("auth_invalid_origin", 403);
    const binding = request.cookies.get(AUTH_BINDING_COOKIE)?.value;
    if (!binding || !/^[A-Za-z0-9_-]{43}$/.test(binding))
        return authFailure("auth_invalid_origin", 403);
    const payload = await readBoundedJson(request);
    if (!payload)
        return authFailure("auth_invalid_request");
    const body = parse(payload);
    if (!body)
        return authFailure("auth_invalid_request");
    return authResponse(await safeApiPost(path, body, { headers: { "X-Newsroom-Auth-Binding": binding, ...serverOwnedMutationOriginHeaders() }, signal: AbortSignal.timeout(15000) }));
}
export function validProvider(value: string): value is "google" | "wechat" | "qq" {
    return ["google", "wechat", "qq"].includes(value);
}
