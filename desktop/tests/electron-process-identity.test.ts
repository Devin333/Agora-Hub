import { describe, expect, it, vi } from "vitest"
import { electronIdentityProtocol, readWindowsElectronProcessIdentity, resolveWindowsPowerShell } from "../src/electron-process-identity"

const windowsEnvironment = { SystemRoot: "C:\\Windows", WINDIR: "C:\\Windows", PATH: "C:\\untrusted" }

describe("readWindowsElectronProcessIdentity", () => {
  it("uses the absolute SystemRoot PowerShell, a fixed no-shell probe, and decimal FILETIME", async () => {
    const execute = vi.fn().mockResolvedValue({ stdout: "133801234567890000" })
    await expect(readWindowsElectronProcessIdentity(4321, execute, windowsEnvironment)).resolves.toEqual({ pid: 4321, creationTime: "133801234567890000" })
    expect(execute).toHaveBeenCalledWith("C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe", [
      "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", electronIdentityProtocol.WINDOWS_IDENTITY_SCRIPT
    ], expect.objectContaining({
      windowsHide: true,
      timeout: 10_000,
      maxBuffer: 4096,
      encoding: "utf8",
      env: expect.objectContaining({ AGORA_ELECTRON_PID: "4321", SystemRoot: "C:\\Windows" })
    }))
    expect(execute.mock.calls[0][2].env).not.toHaveProperty("PATH")
  })

  it("fails closed when SystemRoot is missing, relative, or non-local", () => {
    expect(() => resolveWindowsPowerShell({})).toThrow(/SystemRoot/)
    expect(() => resolveWindowsPowerShell({ SystemRoot: "Windows" })).toThrow(/SystemRoot/)
    expect(() => resolveWindowsPowerShell({ SystemRoot: "\\\\server\\Windows" })).toThrow(/local path/)
  })

  it("fails closed on missing, malformed, or command-injected identities", async () => {
    await expect(readWindowsElectronProcessIdentity(12, vi.fn().mockResolvedValue({ stdout: "123\r\n456" }), windowsEnvironment)).rejects.toMatchObject({ code: "desktop_data_clear_electron_identity_invalid" })
    await expect(readWindowsElectronProcessIdentity(12, vi.fn().mockResolvedValue({ stdout: "123;Remove-Item C:\\" }), windowsEnvironment)).rejects.toMatchObject({ code: "desktop_data_clear_electron_identity_invalid" })
    await expect(readWindowsElectronProcessIdentity(12, vi.fn().mockRejectedValue(new Error("denied")), windowsEnvironment)).rejects.toMatchObject({ code: "desktop_data_clear_electron_identity_unavailable" })
  })
})
