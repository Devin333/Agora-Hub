import { NextRequest } from "next/server"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { DELETE, GET, HEAD, PATCH, POST, PUT } from "./route"

const ORIGINAL_FETCH = globalThis.fetch
const ENV_KEYS = [
  "NEWSROOM_DEPLOYMENT_PROFILE",
  "NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE",
  "NEWSROOM_PUBLIC_ORIGIN",
  "NEWSROOM_ALLOW_INSECURE_ORIGIN",
  "NEWSROOM_API_BASE_URL",
] as const
const ORIGINAL_ENV = Object.fromEntries(ENV_KEYS.map(key => [key, process.env[key]]))

describe("hosted raw API server route", () => {
  beforeEach(() => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "hosted"
    delete process.env.NEXT_PUBLIC_NEWSROOM_DEPLOYMENT_PROFILE
    process.env.NEWSROOM_PUBLIC_ORIGIN = "http://127.0.0.1:3124"
    process.env.NEWSROOM_ALLOW_INSECURE_ORIGIN = "1"
    process.env.NEWSROOM_API_BASE_URL = "http://127.0.0.1:8124"
  })

  afterEach(() => {
    vi.useRealTimers()
    globalThis.fetch = ORIGINAL_FETCH
    vi.restoreAllMocks()
    for (const key of ENV_KEYS) restoreEnv(key, ORIGINAL_ENV[key])
  })

  it("resolves the API origin at request time after a different build-time value", async () => {
    process.env.NEWSROOM_API_BASE_URL = "http://127.0.0.1:8122"
    vi.resetModules()
    const { GET: runtimeGet } = await import("./route")
    process.env.NEWSROOM_API_BASE_URL = "http://127.0.0.1:8124"
    const upstream = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: { initialized: true, session: null } }))
    vi.stubGlobal("fetch", upstream)

    const response = await runtimeGet(rawRequest("/api/v1/auth/session?probe=A%2FB&probe=two"), context("auth", "session"))

    expect(response.status).toBe(200)
    expect(upstream).toHaveBeenCalledOnce()
    const [target, init] = upstream.mock.calls[0] as [URL, RequestInit]
    expect(target.toString()).toBe("http://127.0.0.1:8124/api/v1/auth/session?probe=A%2FB&probe=two")
    expect(init.redirect).toBe("manual")
    expect(new Headers(init.headers).has("x-newsroom-session")).toBe(false)
    expect(await response.json()).toEqual({ success: true, data: { initialized: true, session: null } })
  })

  it("binds the private browser session to the raw auth/session readiness route", async () => {
    const upstream = vi.fn().mockResolvedValue(jsonResponse({
      success: true,
      data: {
        initialized: true,
        session: { user: { userId: "user-1" } },
      },
    }))
    vi.stubGlobal("fetch", upstream)

    const response = await GET(rawRequest("/api/v1/auth/session", {
      headers: { Cookie: "newsroom_session=session-token; renderer_visible=discarded" },
    }), context("auth", "session"))

    const [, init] = upstream.mock.calls[0] as [URL, RequestInit]
    const headers = new Headers(init.headers)
    expect(headers.get("x-newsroom-session")).toBe("session-token")
    expect(headers.has("cookie")).toBe(false)
    expect(await response.json()).toEqual({
      success: true,
      data: {
        initialized: true,
        session: { user: { userId: "user-1" } },
      },
    })
  })

  it("uses the HttpOnly session while rejecting renderer-owned identity headers", async () => {
    const upstream = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: { accepted: true } }))
    vi.stubGlobal("fetch", upstream)
    const body = JSON.stringify({ value: "kept" })
    const request = rawRequest("/api/v1/research/desktop/items/a%20b?revision=2", {
      method: "POST",
      body,
      headers: {
        Authorization: "Bearer renderer-token",
        Connection: "X-Remove-Me",
        "Content-Type": "application/json",
        Cookie: "newsroom_session=session-token; other=secret",
        "If-Match": '"revision-1"',
        Origin: "http://127.0.0.1:3124",
        "Sec-Fetch-Site": "same-origin",
        "X-Api-Client-Id": "forged-client",
        "X-Forwarded-For": "203.0.113.7",
        "X-Forwarded-Host": "foreign.example",
        "X-News-Actor": "forged-owner",
        "X-Newsroom-Request-Id": "req_123",
        "X-Newsroom-Session": "forged-session",
        "X-Remove-Me": "connection-scoped",
      },
    })

    const response = await POST(request, context("research", "desktop", "items", "a b"))

    expect(response.status).toBe(200)
    const [target, init] = upstream.mock.calls[0] as [URL, RequestInit]
    expect(target.toString()).toBe("http://127.0.0.1:8124/api/v1/research/desktop/items/a%20b?revision=2")
    expect(init.method).toBe("POST")
    expect(init.cache).toBe("no-store")
    expect((init as RequestInit & { duplex?: string }).duplex).toBe("half")
    expect(await new Response(init.body).text()).toBe(body)
    const headers = new Headers(init.headers)
    expect(headers.has("authorization")).toBe(false)
    expect(headers.get("x-newsroom-session")).toBe("session-token")
    expect(headers.get("origin")).toBe("http://127.0.0.1:3124")
    expect(headers.get("content-type")).toBe("application/json")
    expect(headers.get("if-match")).toBe('"revision-1"')
    expect(headers.get("x-newsroom-request-id")).toBe("req_123")
    expect(headers.get("accept-encoding")).toBe("identity")
    expect(headers.has("cookie")).toBe(false)
    expect(headers.has("host")).toBe(false)
    expect(headers.has("x-api-client-id")).toBe(false)
    expect(headers.has("x-forwarded-host")).toBe(false)
    // The proxy's address chain reaches the API, which reads it only from the trusted BFF.
    expect(headers.get("x-forwarded-for")).toBe("203.0.113.7")
    expect(headers.has("x-news-actor")).toBe(false)
    expect(headers.has("x-remove-me")).toBe(false)
  })

  it.each([
    [POST, "POST"],
    [PUT, "PUT"],
    [PATCH, "PATCH"],
    [DELETE, "DELETE"],
  ] as const)("rejects a foreign Origin for %s before forwarding", async (handler, method) => {
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)
    const response = await handler(rawRequest("/api/v1/research/items/one", {
      method,
      body: method === "DELETE" ? undefined : "{}",
      headers: {
        Origin: "https://foreign.example",
        "Sec-Fetch-Site": "cross-site",
      },
    }), context("research", "items", "one"))

    expect(response.status).toBe(403)
    expect(await response.json()).toEqual({ success: false, error: { code: "auth_invalid_origin" } })
    expect(upstream).not.toHaveBeenCalled()
  })

  it.each([4, 8])("fails closed on %i-layer traversal and control encodings", async depth => {
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)
    for (const value of ["..", "/", "\\", "\u0000", "\u001f", "%GG"]) {
      const response = await GET(rawRequest("/api/v1/research/rejected"), context("research", deeplyEncode(value, depth)))
      expect(response.status).toBe(400)
      expect(await response.json()).toEqual({ success: false, error: { code: "raw_api_path_invalid" } })
    }
    expect(upstream).not.toHaveBeenCalled()
  })

  it("preserves stable decoded path text and rejects ambiguous literal or malformed percent syntax", async () => {
    const upstream = vi.fn().mockImplementation(() => jsonResponse({ success: true }))
    vi.stubGlobal("fetch", upstream)
    const response = await GET(rawRequest("/api/v1/research/paper%20title"), context("research", "paper title"))
    expect(response.status).toBe(200)
    expect(await response.json()).toEqual({ success: true })
    expect((upstream.mock.calls[0][0] as URL).pathname).toBe("/api/v1/research/paper%20title")
    for (const value of ["100%", "%", "%2", "%GG", "%25"]) {
      // Already-decoded params must reach a fixed point within the budget;
      // literal '%' is deliberately rejected rather than guessed as encoding.
      const rejected = await GET(rawRequest("/api/v1/research/rejected"), context("research", value))
      expect(rejected.status).toBe(400)
    }
    expect(upstream).toHaveBeenCalledOnce()
  })

  it.each([
    "/api/v1/research/%252525252e%252525252e/admin",
    "/api/v1/research/%252525252fadmin",
    "/api/v1/research/%252525255cadmin",
    "/api/v1/research/%2525252500",
    "/api/v1/research/items/%2e%2e/other",
    "/api/v1/research/items/../other",
    "/api/v1/admin/diagnostics",
    "http://user:pass@127.0.0.1:8124/api/v1/research/items",
  ])("rejects unsafe or unsupported redirect %s and cancels its body", async location => {
    const cancel = vi.fn()
    const upstream = vi.fn().mockResolvedValue(new Response(new ReadableStream({ cancel }), {
      status: 307,
      headers: { Location: location },
    }))
    vi.stubGlobal("fetch", upstream)
    const response = await GET(rawRequest("/api/v1/research/items/one"), context("research", "items", "one"))
    expect(response.status).toBe(502)
    expect(await response.json()).toEqual({ success: false, error: { code: "api_redirect_forbidden" } })
    expect(cancel).toHaveBeenCalledOnce()
    expect((upstream.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true)
  })

  it("aborts the actual fetch at the finite deadline and returns a deterministic 504", async () => {
    vi.useFakeTimers()
    let signal: AbortSignal | undefined
    const upstream = vi.fn((_target: URL, init: RequestInit) => {
      signal = init.signal as AbortSignal
      return new Promise<Response>((_resolve, reject) => {
        signal!.addEventListener("abort", () => reject(signal!.reason), { once: true })
      })
    })
    vi.stubGlobal("fetch", upstream)
    const pending = GET(rawRequest("/api/v1/auth/session"), context("auth", "session"))
    await vi.advanceTimersByTimeAsync(60_000)
    const response = await pending
    expect(signal?.aborted).toBe(true)
    expect(response.status).toBe(504)
    expect(await response.json()).toEqual({ success: false, error: { code: "api_upstream_timeout" } })
    expect(vi.getTimerCount()).toBe(0)
  })

  it("cancels an actual stalled upstream response stream at the same deadline", async () => {
    vi.useFakeTimers()
    const cancel = vi.fn()
    const upstream = vi.fn().mockResolvedValue(new Response(new ReadableStream({ cancel }), {
      status: 206,
      headers: { "Content-Type": "application/pdf" },
    }))
    vi.stubGlobal("fetch", upstream)
    const response = await GET(rawRequest("/api/v1/research/blob"), context("research", "blob"))
    const bytes = response.arrayBuffer()
    const failure = expect(bytes).rejects.toMatchObject({ name: "TimeoutError" })
    await vi.advanceTimersByTimeAsync(60_000)
    await failure
    expect(response.status).toBe(206)
    expect(cancel).toHaveBeenCalledOnce()
    expect((upstream.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })

  it("aborts the upstream and cancels its reader when the downstream consumer cancels", async () => {
    vi.useFakeTimers()
    const cancel = vi.fn()
    const upstream = vi.fn().mockResolvedValue(new Response(new ReadableStream({ cancel })))
    vi.stubGlobal("fetch", upstream)
    const response = await GET(rawRequest("/api/v1/research/blob"), context("research", "blob"))
    await response.body!.cancel("consumer stopped")
    expect(cancel).toHaveBeenCalledOnce()
    expect((upstream.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })

  it("propagates a client abort to the actual upstream response stream", async () => {
    vi.useFakeTimers()
    const client = new AbortController()
    const cancel = vi.fn()
    const upstream = vi.fn().mockResolvedValue(new Response(new ReadableStream({ cancel })))
    vi.stubGlobal("fetch", upstream)
    const response = await GET(rawRequest("/api/v1/research/blob", { signal: client.signal }), context("research", "blob"))
    const bytes = response.arrayBuffer()
    const failure = expect(bytes).rejects.toMatchObject({ name: "AbortError" })
    client.abort(new DOMException("Client disconnected", "AbortError"))
    await failure
    expect(cancel).toHaveBeenCalledOnce()
    expect((upstream.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true)
    expect(vi.getTimerCount()).toBe(0)
  })

  it("cleans the deadline after an upstream connection failure and returns 503", async () => {
    vi.useFakeTimers()
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("connect failed")))
    const response = await GET(rawRequest("/api/v1/auth/session"), context("auth", "session"))
    expect(response.status).toBe(503)
    expect(await response.json()).toEqual({ success: false, error: { code: "api_service_unavailable" } })
    expect(vi.getTimerCount()).toBe(0)
  })

  it("disposes its deadline after a successful stream without changing response bytes", async () => {
    vi.useFakeTimers()
    const upstream = vi.fn().mockResolvedValue(new Response(new Uint8Array([0, 1, 255])))
    vi.stubGlobal("fetch", upstream)
    const response = await GET(rawRequest("/api/v1/research/blob"), context("research", "blob"))
    expect([...new Uint8Array(await response.arrayBuffer())]).toEqual([0, 1, 255])
    expect(vi.getTimerCount()).toBe(0)
    expect((upstream.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(false)
  })

  it("requires the configured canonical Host even for a read without Origin", async () => {
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)

    const response = await GET(new NextRequest("http://localhost:3124/api/v1/auth/session", {
      headers: { Host: "localhost:3124" },
    }), context("auth", "session"))

    expect(response.status).toBe(403)
    expect(upstream).not.toHaveBeenCalled()
  })

  it.each([
    [[".."], "raw_api_path_invalid", 400],
    [["%252e%252e"], "raw_api_path_invalid", 400],
    [["research", "items%252Fadmin"], "raw_api_path_invalid", 400],
  ] as const)("rejects unsafe path segments %j", async (segments, code, status) => {
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)

    const response = await GET(rawRequest("/api/v1/rejected"), context(...segments))

    expect(response.status).toBe(status)
    expect(await response.json()).toEqual({ success: false, error: { code } })
    expect(upstream).not.toHaveBeenCalled()
  })

  it("keeps raw authentication mutations behind the existing BFF", async () => {
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)

    const response = await POST(rawRequest("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: "reader", password: "password" }),
      headers: {
        Origin: "http://127.0.0.1:3124",
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
      },
    }), context("auth", "login"))

    expect(response.status).toBe(404)
    expect(await response.json()).toEqual({ success: false, error: { code: "raw_api_path_unsupported" } })
    expect(upstream).not.toHaveBeenCalled()
  })

  it("does not expose operator or unrelated API namespaces to the renderer", async () => {
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)

    const response = await GET(rawRequest("/api/v1/admin/diagnostics"), context("admin", "diagnostics"))

    expect(response.status).toBe(404)
    expect(await response.json()).toEqual({ success: false, error: { code: "raw_api_path_unsupported" } })
    expect(upstream).not.toHaveBeenCalled()
  })

  it.each([
    ["GET", ["account"]],
    ["PUT", ["account", "model-credential"]],
    ["DELETE", ["account", "model-credential"]],
    ["POST", ["account", "model-credential", "verify"]],
    ["GET", ["admin", "accounts"]],
    ["POST", ["admin", "accounts"]],
    ["POST", ["admin", "accounts", "user_0123456789abcdef", "disable"]],
    ["POST", ["admin", "accounts", "user_0123456789abcdef", "password"]],
    ["PUT", ["admin", "accounts", "user_0123456789abcdef", "role"]],
  ] as const)("forwards the reviewed account route %s %j with the session and public Origin", async (method, path) => {
    const upstream = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: {} }))
    vi.stubGlobal("fetch", upstream)
    const handler = { GET, POST, PUT, DELETE }[method]
    const response = await handler(rawRequest(`/api/v1/${path.join("/")}`, {
      method,
      body: method === "GET" || method === "DELETE" ? undefined : "{}",
      headers: { Cookie: "newsroom_session=session-token", Origin: "http://127.0.0.1:3124", "Sec-Fetch-Site": "same-origin" },
    }), context(...path))

    expect(response.status).toBe(200)
    const [target, init] = upstream.mock.calls[0] as [URL, RequestInit]
    expect(target.pathname).toBe(`/api/v1/${path.join("/")}`)
    const headers = new Headers(init.headers)
    expect(headers.get("x-newsroom-session")).toBe("session-token")
    if (method !== "GET") expect(headers.get("origin")).toBe("http://127.0.0.1:3124")
  })

  it.each([
    ["POST", ["account", "password"]],
    ["DELETE", ["admin", "accounts", "user_0123456789abcdef"]],
    ["POST", ["admin", "accounts", "not-an-account-id", "disable"]],
    ["GET", ["account", "model-credential", "verify"]],
  ] as const)("refuses the unreviewed account route %s %j", async (method, path) => {
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)
    const handler = { GET, POST, DELETE }[method]
    const response = await handler(rawRequest(`/api/v1/${path.join("/")}`, {
      method,
      body: method === "POST" ? "{}" : undefined,
      headers: { Origin: "http://127.0.0.1:3124", "Sec-Fetch-Site": "same-origin" },
    }), context(...path))

    expect(response.status).toBe(404)
    expect(upstream).not.toHaveBeenCalled()
  })

  it("is unavailable outside the hosted profile", async () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "default"
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)

    const response = await GET(rawRequest("/api/v1/account"), context("account"))

    expect(response.status).toBe(404)
    expect(await response.json()).toEqual({ success: false, error: { code: "raw_api_route_unavailable" } })
    expect(upstream).not.toHaveBeenCalled()
  })

  it("rejects an oversized target before forwarding", async () => {
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)
    const response = await GET(rawRequest(`/api/v1/auth/session?query=${"a".repeat(17 * 1024)}`), context("auth", "session"))

    expect(response.status).toBe(414)
    expect(await response.json()).toEqual({ success: false, error: { code: "raw_api_target_too_long" } })
    expect(upstream).not.toHaveBeenCalled()
  })

  it("preserves status, body, response headers, and Set-Cookie values", async () => {
    const headers = new Headers({
      Connection: "keep-alive, X-Remove-Me",
      "Content-Range": "bytes 0-3/8",
      "Content-Type": "application/octet-stream",
      "X-Request-Id": "request-1",
      "X-Remove-Me": "connection-scoped",
    })
    headers.append("Set-Cookie", "first=one; Path=/; HttpOnly")
    headers.append("Set-Cookie", "second=two; Path=/; SameSite=Lax")
    const upstream = vi.fn().mockResolvedValue(new Response(new Uint8Array([0, 1, 2, 3]), {
      status: 206,
      statusText: "Partial Content",
      headers,
    }))
    vi.stubGlobal("fetch", upstream)

    const response = await GET(rawRequest("/api/v1/research/blob", {
      headers: { Cookie: "newsroom_session=session-token" },
    }), context("research", "blob"))

    expect(response.status).toBe(206)
    expect(response.statusText).toBe("Partial Content")
    expect([...new Uint8Array(await response.arrayBuffer())]).toEqual([0, 1, 2, 3])
    expect(response.headers.get("content-range")).toBe("bytes 0-3/8")
    expect(response.headers.get("content-type")).toBe("application/octet-stream")
    expect(response.headers.get("x-request-id")).toBe("request-1")
    expect(response.headers.has("connection")).toBe(false)
    expect(response.headers.has("x-remove-me")).toBe(false)
    expect(response.headers.get("set-cookie")).toContain("first=one")
    expect(response.headers.get("set-cookie")).toContain("second=two")
  })

  it("rewrites same-API redirects to the frontend origin and rejects foreign redirects", async () => {
    const sameApi = vi.fn().mockResolvedValue(new Response(null, {
      status: 307,
      headers: { Location: "http://127.0.0.1:8124/api/v1/research/items/two?next=1#result" },
    }))
    vi.stubGlobal("fetch", sameApi)
    const accepted = await GET(rawRequest("/api/v1/research/items/one"), context("research", "items", "one"))
    expect(accepted.status).toBe(307)
    expect(accepted.headers.get("location")).toBe("/api/v1/research/items/two?next=1#result")

    const foreign = vi.fn().mockResolvedValue(new Response(null, {
      status: 302,
      headers: { Location: "https://foreign.example/collect" },
    }))
    vi.stubGlobal("fetch", foreign)
    const rejected = await GET(rawRequest("/api/v1/research/items/one"), context("research", "items", "one"))
    expect(rejected.status).toBe(502)
    expect(rejected.headers.has("location")).toBe(false)
    expect(await rejected.json()).toEqual({ success: false, error: { code: "api_redirect_forbidden" } })
  })

  it("keeps HEAD bodyless while preserving the upstream status and headers", async () => {
    const upstream = vi.fn().mockResolvedValue(new Response("ignored", {
      status: 200,
      headers: { ETag: '"paper-1"' },
    }))
    vi.stubGlobal("fetch", upstream)

    const response = await HEAD(rawRequest("/api/v1/research/items/one", { method: "HEAD" }), context("research", "items", "one"))

    expect(response.status).toBe(200)
    expect(response.headers.get("etag")).toBe('"paper-1"')
    expect(await response.text()).toBe("")
  })

  it("is unavailable in the default deployment profile and never selects an upstream", async () => {
    process.env.NEWSROOM_DEPLOYMENT_PROFILE = "default"
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)

    const response = await GET(rawRequest("/api/v1/auth/session"), context("auth", "session"))

    expect(response.status).toBe(404)
    expect(upstream).not.toHaveBeenCalled()
  })

  it("reaches a private-network API origin such as the compose service name", async () => {
    process.env.NEWSROOM_API_BASE_URL = "http://api:8000"
    const upstream = vi.fn().mockResolvedValue(jsonResponse({ success: true, data: { initialized: true, session: null } }))
    vi.stubGlobal("fetch", upstream)

    const response = await GET(rawRequest("/api/v1/auth/session"), context("auth", "session"))

    expect(response.status).toBe(200)
    expect((upstream.mock.calls[0] as [URL])[0].toString()).toBe("http://api:8000/api/v1/auth/session")
  })

  it("fails closed when the runtime API origin is not a pure origin", async () => {
    process.env.NEWSROOM_API_BASE_URL = "http://api:8000/api"
    const upstream = vi.fn()
    vi.stubGlobal("fetch", upstream)

    const response = await GET(rawRequest("/api/v1/auth/session"), context("auth", "session"))

    expect(response.status).toBe(503)
    expect(await response.json()).toEqual({ success: false, error: { code: "api_base_invalid" } })
    expect(upstream).not.toHaveBeenCalled()
  })
})

function rawRequest(
  path: string,
  options: { method?: string; body?: BodyInit; headers?: HeadersInit; signal?: AbortSignal } = {},
): NextRequest {
  const headers = new Headers(options.headers)
  if (!headers.has("Host")) headers.set("Host", "127.0.0.1:3124")
  return new NextRequest(`http://127.0.0.1:3124${path}`, {
    method: options.method,
    body: options.body,
    headers,
    signal: options.signal,
  })
}

function context(...path: string[]) {
  return { params: Promise.resolve({ path }) }
}

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })
}

function restoreEnv(name: string, value: string | undefined) {
  if (value === undefined) delete process.env[name]
  else process.env[name] = value
}

function deeplyEncode(value: string, depth: number) {
  let encoded = [...value].map(char => `%${char.charCodeAt(0).toString(16).padStart(2, "0")}`).join("")
  for (let layer = 1; layer < depth; layer += 1) encoded = encodeURIComponent(encoded)
  return encoded
}
