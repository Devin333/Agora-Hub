import path from "node:path"
import { execFile } from "node:child_process"
import { promisify } from "node:util"
import type { ElectronProcessIdentity } from "./owned-data-clear"

const execFileAsync = promisify(execFile)
const CREATION_TIME = /^[1-9][0-9]{0,31}$/
const WINDOWS_IDENTITY_SCRIPT = [
  "$ErrorActionPreference='Stop'",
  "$p=Get-Process -Id ([int]$env:AGORA_ELECTRON_PID)",
  "[Console]::Out.Write($p.StartTime.ToUniversalTime().ToFileTimeUtc().ToString([Globalization.CultureInfo]::InvariantCulture))"
].join(";")

export type ExecuteIdentityProbe = (
  file: string,
  args: readonly string[],
  options: { windowsHide: true; timeout: number; maxBuffer: number; encoding: "utf8"; env: NodeJS.ProcessEnv }
) => Promise<{ stdout: string }>

async function defaultExecute(file: string, args: readonly string[], options: Parameters<ExecuteIdentityProbe>[2]): Promise<{ stdout: string }> {
  const result = await execFileAsync(file, [...args], options)
  return { stdout: result.stdout }
}

export class ElectronProcessIdentityError extends Error {
  constructor(readonly code: string, message: string) {
    super(message)
    this.name = "ElectronProcessIdentityError"
  }
}

export function resolveWindowsPowerShell(environment: NodeJS.ProcessEnv): string {
  const root = environment.SystemRoot ?? environment.SYSTEMROOT ?? environment.WINDIR
  if (!root || !path.win32.isAbsolute(root)) {
    throw new ElectronProcessIdentityError("desktop_data_clear_electron_identity_unavailable", "Windows SystemRoot is unavailable")
  }
  const normalizedRoot = path.win32.normalize(root)
  if (!/^[A-Za-z]:\\/.test(normalizedRoot)) {
    throw new ElectronProcessIdentityError("desktop_data_clear_electron_identity_unavailable", "Windows SystemRoot is not a fixed local path")
  }
  return path.win32.join(normalizedRoot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
}

export async function readWindowsElectronProcessIdentity(
  pid = process.pid,
  execute: ExecuteIdentityProbe = defaultExecute,
  environment: NodeJS.ProcessEnv = process.env
): Promise<ElectronProcessIdentity> {
  if (process.platform !== "win32" && execute === defaultExecute) {
    throw new ElectronProcessIdentityError("desktop_data_clear_platform_unsupported", "Owned data clear requires the Windows process identity probe")
  }
  if (!Number.isSafeInteger(pid) || pid < 1 || pid > 2 ** 31 - 1) throw new ElectronProcessIdentityError("desktop_data_clear_electron_identity_invalid", "Electron PID is invalid")
  let stdout: string
  try {
    const powerShell = resolveWindowsPowerShell(environment)
    const env: NodeJS.ProcessEnv = {
      AGORA_ELECTRON_PID: String(pid),
      SystemRoot: environment.SystemRoot ?? environment.SYSTEMROOT ?? environment.WINDIR,
      WINDIR: environment.WINDIR ?? environment.SystemRoot ?? environment.SYSTEMROOT
    }
    for (const name of ["TEMP", "TMP"] as const) if (environment[name]) env[name] = environment[name]
    ;({ stdout } = await execute(powerShell, ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", WINDOWS_IDENTITY_SCRIPT], {
      windowsHide: true,
      timeout: 10_000,
      maxBuffer: 4096,
      encoding: "utf8",
      env
    }))
  } catch (error) {
    if (error instanceof ElectronProcessIdentityError) throw error
    throw new ElectronProcessIdentityError("desktop_data_clear_electron_identity_unavailable", "Windows did not return the exact Electron creation identity")
  }
  const creationTime = stdout.trim()
  if (!CREATION_TIME.test(creationTime)) throw new ElectronProcessIdentityError("desktop_data_clear_electron_identity_invalid", "Windows returned an invalid Electron creation identity")
  return { pid, creationTime }
}

export const electronIdentityProtocol = { WINDOWS_IDENTITY_SCRIPT }
