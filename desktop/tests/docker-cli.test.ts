import path from "node:path"
import { describe, expect, it } from "vitest"
import { classifyDockerFailure, createDockerRunner, dockerEnvironment, locateDocker, type DockerLocatorEnvironment } from "../src/docker-cli"

function environment(platform: NodeJS.Platform, env: NodeJS.ProcessEnv, existing: string[]): DockerLocatorEnvironment {
  return { platform, env, exists: file => existing.includes(file) }
}

describe("locating Docker", () => {
  it("finds Docker Desktop on Windows before anything on PATH", () => {
    const desktop = "C:\\Program Files\\Docker\\Docker\\resources\\bin\\docker.exe"
    expect(locateDocker(environment("win32", { ProgramFiles: "C:\\Program Files", PATH: "C:\\tools" }, [desktop, "C:\\tools\\docker.exe"]))).toBe(desktop)
    expect(locateDocker(environment("win32", { ProgramFiles: "C:\\Program Files", Path: "C:\\tools;C:\\other" }, ["C:\\tools\\docker.exe"]))).toBe("C:\\tools\\docker.exe")
  })

  it("finds Docker Desktop and Homebrew installs on macOS, which a Dock launch does not have on PATH", () => {
    expect(locateDocker(environment("darwin", { PATH: "/usr/bin:/bin" }, ["/opt/homebrew/bin/docker"]))).toBe("/opt/homebrew/bin/docker")
    expect(locateDocker(environment("darwin", { HOME: "/Users/ada", PATH: "/usr/bin" }, ["/Users/ada/.docker/bin/docker"]))).toBe("/Users/ada/.docker/bin/docker")
    expect(locateDocker(environment("darwin", { PATH: "/usr/bin" }, ["/Applications/Docker.app/Contents/Resources/bin/docker"]))).toBe("/Applications/Docker.app/Contents/Resources/bin/docker")
  })

  it("reports no Docker when none is installed", () => {
    expect(locateDocker(environment("linux", { PATH: "/usr/bin:/bin" }, []))).toBeNull()
  })
})

describe("Docker command environment", () => {
  it("passes what Docker needs and nothing a user's shell may hold", () => {
    const env = dockerEnvironment("/opt/homebrew/bin/docker", environment("darwin", {
      HOME: "/Users/ada",
      PATH: "/usr/bin",
      DOCKER_HOST: "unix:///Users/ada/.colima/docker.sock",
      OPENAI_API_KEY: "sk-secret",
      ANTHROPIC_API_KEY: "sk-ant-secret",
      AWS_SECRET_ACCESS_KEY: "secret",
      NEWSROOM_CREDENTIAL_ENCRYPTION_KEY: "leaked"
    }, []))
    expect(env.HOME).toBe("/Users/ada")
    expect(env.DOCKER_HOST).toBe("unix:///Users/ada/.colima/docker.sock")
    expect(env.PATH?.split(":")[0]).toBe("/opt/homebrew/bin")
    expect(env.PATH).toContain("/usr/bin")
    expect(env.COMPOSE_ANSI).toBe("never")
    expect(Object.keys(env).filter(name => /KEY|SECRET|TOKEN/.test(name))).toEqual([])
  })
})

describe("Docker failure categories", () => {
  it.each([
    ["Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?", "docker_not_running"],
    ["error during connect: open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.", "docker_not_running"],
    ["permission denied while trying to connect to the Docker daemon socket", "docker_permission"],
    ["Bind for 127.0.0.1:45180 failed: port is already allocated", "local_port_in_use"],
    ["manifest for ghcr.io/devin333/agora-api:v9.9.9 not found: manifest unknown", "image_unavailable"],
    ["Error response from daemon: Head \"https://ghcr.io/v2/...\": denied: denied", "image_unavailable"],
    ["Get \"https://ghcr.io/v2/\": net/http: TLS handshake timeout", "image_pull_failed"],
    ["write /var/lib/docker/tmp/x: no space left on device", "disk_full"],
    ["container agora-desktop-api-1 is unhealthy", "local_stack_failed"]
  ])("reads %s as %s", (output, code) => {
    expect(classifyDockerFailure(output)).toBe(code)
  })
})

describe("Docker runner", () => {
  // Node itself stands in for the docker executable.
  const node = process.execPath
  const runner = createDockerRunner(node, { platform: process.platform, env: { ...process.env, PATH: path.dirname(node) }, exists: () => true })

  it("streams output by line, writes stdin and reports the exit code", async () => {
    const lines: string[] = []
    const result = await runner(["-e", "process.stdin.on('data', d => { process.stdout.write('got ' + d.toString().trim() + '\\nsecond'); process.stderr.write('warn\\n'); process.exit(3) })"], {
      input: "secret-password\n",
      timeoutMs: 20_000,
      onLine: line => lines.push(line)
    })
    expect(result).toMatchObject({ code: 3, timedOut: false, aborted: false })
    expect(result.stdout).toBe("got secret-password\nsecond")
    expect(lines.sort()).toEqual(["got secret-password", "second", "warn"])
  })

  it("ends a command that passes its deadline", async () => {
    const result = await runner(["-e", "setInterval(() => {}, 1000)"], { timeoutMs: 300 })
    expect(result.timedOut).toBe(true)
    expect(result.code).not.toBe(0)
  })

  it("ends a command the caller aborts", async () => {
    const controller = new AbortController()
    const pending = runner(["-e", "setInterval(() => {}, 1000)"], { timeoutMs: 20_000, signal: controller.signal })
    setTimeout(() => controller.abort(), 100)
    await expect(pending).resolves.toMatchObject({ aborted: true })
  })
})
