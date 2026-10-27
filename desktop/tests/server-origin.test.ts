import { describe, expect, it } from "vitest"
import { parseServerOrigin, serverOriginErrorMessage, SERVER_ORIGIN_ERROR_MESSAGES } from "../src/server-origin"

const packaged = { allowLoopbackHttp: false }
const development = { allowLoopbackHttp: true }

describe("server origin", () => {
  it("reduces an HTTPS address to its origin and assumes HTTPS for a bare host", () => {
    expect(parseServerOrigin("https://agora.example.com", packaged)).toEqual({ ok: true, origin: "https://agora.example.com" })
    expect(parseServerOrigin("  https://Agora.Example.com/  ", packaged)).toEqual({ ok: true, origin: "https://agora.example.com" })
    expect(parseServerOrigin("agora.example.com", packaged)).toEqual({ ok: true, origin: "https://agora.example.com" })
    expect(parseServerOrigin("agora.example.com:8443", packaged)).toEqual({ ok: true, origin: "https://agora.example.com:8443" })
    expect(parseServerOrigin("https://agora.example.com:443", packaged)).toEqual({ ok: true, origin: "https://agora.example.com" })
    expect(parseServerOrigin("https://203.0.113.7.sslip.io", packaged)).toEqual({ ok: true, origin: "https://203.0.113.7.sslip.io" })
  })

  it("rejects what a bare origin cannot express instead of dropping it", () => {
    const code = (value: unknown, policy = packaged) => {
      const result = parseServerOrigin(value, policy)
      return result.ok ? "accepted" : result.code
    }
    expect(code("")).toBe("server_url_empty")
    expect(code("   ")).toBe("server_url_empty")
    expect(code(42)).toBe("server_url_invalid")
    expect(code("https://")).toBe("server_url_invalid")
    expect(code("https://agora example.com")).toBe("server_url_invalid")
    expect(code("ftp://agora.example.com")).toBe("server_url_scheme_unsupported")
    expect(code("file:///C:/agora")).toBe("server_url_scheme_unsupported")
    expect(code("https://admin:secret@agora.example.com")).toBe("server_url_has_credentials")
    expect(code("https://agora.example.com/paper-agent")).toBe("server_url_has_path")
    expect(code("https://agora.example.com/?team=1")).toBe("server_url_has_path")
    expect(code("https://agora.example.com/#login")).toBe("server_url_has_path")
    expect(code("https://agora.example.com?")).toBe("server_url_has_path")
  })

  it("requires HTTPS except for a loopback host in a development build", () => {
    const code = (value: string, policy: { allowLoopbackHttp: boolean }) => {
      const result = parseServerOrigin(value, policy)
      return result.ok ? result.origin : result.code
    }
    expect(code("http://agora.example.com", development)).toBe("server_url_insecure")
    expect(code("http://203.0.113.7", development)).toBe("server_url_insecure")
    expect(code("http://127.0.0.1:3317", packaged)).toBe("server_url_insecure")
    expect(code("http://127.0.0.1:3317", development)).toBe("http://127.0.0.1:3317")
    expect(code("http://localhost:3000", development)).toBe("http://localhost:3000")
    expect(code("http://[::1]:3000", development)).toBe("http://[::1]:3000")
  })

  it("has a fixed Chinese explanation for every rejection", () => {
    for (const code of Object.keys(SERVER_ORIGIN_ERROR_MESSAGES) as Array<keyof typeof SERVER_ORIGIN_ERROR_MESSAGES>) {
      expect(serverOriginErrorMessage(code)).toMatch(/[\u4e00-\u9fff]/)
    }
  })
})
