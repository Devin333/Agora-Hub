import { describe, expect, it, vi } from "vitest"
import { chromiumErrorName, describeHttpStatus, describeNetworkError, isSupersededLoad } from "../src/load-failure"
import { classifyHealthResponse, probeServer, type ProbeFetch } from "../src/server-probe"

const healthy = JSON.stringify({ success: true, data: { status: "ok", service: "agora-hub-frontend", deploymentProfile: "hosted", ready: true } })

describe("load failures", () => {
  it("maps Chromium network errors to fixed categories", () => {
    expect(chromiumErrorName("net::ERR_NAME_NOT_RESOLVED")).toBe("ERR_NAME_NOT_RESOLVED")
    expect(describeNetworkError("net::ERR_INTERNET_DISCONNECTED").code).toBe("offline")
    expect(describeNetworkError("net::ERR_NAME_NOT_RESOLVED").code).toBe("dns")
    expect(describeNetworkError("net::ERR_CONNECTION_REFUSED").code).toBe("refused")
    expect(describeNetworkError("net::ERR_CONNECTION_TIMED_OUT").code).toBe("timeout")
    expect(describeNetworkError("net::ERR_CONNECTION_RESET").code).toBe("reset")
    expect(describeNetworkError("net::ERR_CERT_DATE_INVALID").code).toBe("certificate")
    expect(describeNetworkError("net::ERR_CERT_AUTHORITY_INVALID").code).toBe("certificate")
    expect(describeNetworkError("net::ERR_SSL_PROTOCOL_ERROR").code).toBe("tls")
    expect(describeNetworkError("net::ERR_PROXY_CONNECTION_FAILED").code).toBe("proxy")
    expect(describeNetworkError("something odd").code).toBe("unexpected")
  })

  it("never retries on its own what only the user or the administrator can fix", () => {
    expect(describeNetworkError("net::ERR_CERT_DATE_INVALID").retryable).toBe(false)
    expect(describeNetworkError("net::ERR_SSL_PROTOCOL_ERROR").retryable).toBe(false)
    expect(describeNetworkError("net::ERR_CONNECTION_REFUSED").retryable).toBe(true)
    expect(describeHttpStatus(404)?.retryable).toBe(false)
  })

  it("shows fixed Chinese text and never the upstream description", () => {
    const failure = describeNetworkError("net::ERR_CONNECTION_REFUSED (secret upstream detail)")
    expect(failure.message).toMatch(/[一-鿿]/)
    expect(failure.message).not.toContain("secret")
  })

  it("treats server errors as unavailable and an aborted load as superseded", () => {
    expect(describeHttpStatus(200)).toBeNull()
    expect(describeHttpStatus(401)).toBeNull()
    expect(describeHttpStatus(502)?.code).toBe("server_unavailable")
    expect(describeHttpStatus(404)?.code).toBe("not_agora")
    expect(isSupersededLoad("net::ERR_ABORTED")).toBe(true)
    expect(isSupersededLoad("net::ERR_FAILED")).toBe(false)
  })
})

describe("server probe", () => {
  it("accepts only a ready hosted Agora web client", () => {
    expect(classifyHealthResponse(200, healthy)).toBeNull()
    expect(classifyHealthResponse(200, healthy.replace('"hosted"', '"default"'))?.code).toBe("not_agora")
    expect(classifyHealthResponse(200, healthy.replace("agora-hub-frontend", "other"))?.code).toBe("not_agora")
    expect(classifyHealthResponse(200, healthy.replace('"ready":true', '"ready":false'))?.code).toBe("server_unavailable")
    expect(classifyHealthResponse(200, "<html>welcome</html>")?.code).toBe("not_agora")
    expect(classifyHealthResponse(302, "")?.code).toBe("not_agora")
    expect(classifyHealthResponse(503, "")?.code).toBe("server_unavailable")
  })

  it("asks the health route without following redirects or caching", async () => {
    const fetch = vi.fn<ProbeFetch>(async () => ({ status: 200, text: async () => healthy }))
    await expect(probeServer(fetch, "https://agora.example.com")).resolves.toBeNull()
    expect(fetch).toHaveBeenCalledWith("https://agora.example.com/api/health", expect.objectContaining({ redirect: "manual", cache: "no-store" }))
  })

  it("reports network failures and timeouts by category", async () => {
    await expect(probeServer(async () => { throw new Error("net::ERR_NAME_NOT_RESOLVED") }, "https://agora.example.com")).resolves.toMatchObject({ code: "dns" })
    const hanging: ProbeFetch = (_url, init) => new Promise((_resolve, reject) => init.signal.addEventListener("abort", () => reject(new Error("aborted"))))
    await expect(probeServer(hanging, "https://agora.example.com", 20)).resolves.toMatchObject({ code: "timeout" })
  })
})
