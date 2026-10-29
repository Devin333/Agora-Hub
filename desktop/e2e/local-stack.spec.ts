import { mkdtemp, readFile, rm } from "node:fs/promises"
import os from "node:os"
import path from "node:path"
import { expect, test } from "@playwright/test"
import { createDockerRunner, locateDocker, systemDockerEnvironment } from "../src/docker-cli"
import { COMPOSE_FILE, ENV_FILE, testStackProjectName } from "../src/local-stack"
import { launch, quit } from "./harness"

/**
 * Runs the local stack for real: Docker, the compose file the build packages and
 * the server images. It needs images already on this computer, named
 * `<registry>/agora-api:<tag>` and `<registry>/agora-web:<tag>`, whose web image
 * reports that tag, for example:
 *
 *   AGORA_DESKTOP_E2E_IMAGE_REGISTRY=local AGORA_DESKTOP_E2E_IMAGE_TAG=v0.0.0-check npm run test:e2e
 *
 * Without both variables the test is skipped.
 */
const imageRegistry = process.env.AGORA_DESKTOP_E2E_IMAGE_REGISTRY?.trim() ?? ""
const imageTag = process.env.AGORA_DESKTOP_E2E_IMAGE_TAG?.trim() ?? ""
const launchEnvironment = { AGORA_DESKTOP_IMAGE_REGISTRY: imageRegistry, AGORA_DESKTOP_IMAGE_TAG: imageTag }
const ADMIN = { username: "owner", password: "local-stack-e2e" }

/** Removes the test profile's containers, network and data volume. */
async function removeStack(userData: string): Promise<void> {
  const environment = systemDockerEnvironment()
  const executable = locateDocker(environment)
  if (!executable) return
  const directory = path.join(userData, "stack")
  const result = await createDockerRunner(executable, environment)([
    "compose",
    "--project-name", testStackProjectName(userData),
    "--project-directory", directory,
    "--env-file", path.join(directory, ENV_FILE),
    "--file", path.join(directory, COMPOSE_FILE),
    "down", "--volumes", "--remove-orphans", "--timeout", "30"
  ], { cwd: directory, timeoutMs: 180_000 })
  if (result.code !== 0) console.warn(`local stack cleanup failed: ${result.stderr}`)
}

test.describe("Agora Paper Agent local stack", () => {
  test.skip(!imageRegistry || !imageTag, "set AGORA_DESKTOP_E2E_IMAGE_REGISTRY and AGORA_DESKTOP_E2E_IMAGE_TAG to run the stack on Docker")
  test.setTimeout(900_000)

  let userData: string

  test.beforeEach(async () => {
    userData = await mkdtemp(path.join(os.tmpdir(), "agora-electron-local-"))
  })

  test.afterEach(async () => {
    await removeStack(userData)
    await rm(userData, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
  })

  test("starts the stack on this computer, creates its administrator and reopens it on the next launch", async () => {
    const first = await launch(userData, launchEnvironment)
    let origin: string
    try {
      const page = first.page
      await expect(page.locator("#title")).toHaveText("选择连接方式")
      await page.locator("input[name=mode][value=local]").check()
      await expect(page.locator("#local-fields")).toBeVisible()
      await page.locator("#choose").click()

      // A fresh stack has no account, so the app asks for its first administrator.
      await expect(page.locator("#admin-form")).toBeVisible({ timeout: 600_000 })
      await expect(page.locator("#local-steps li")).not.toHaveCount(0)

      await page.locator("#admin-username").fill(ADMIN.username)
      await page.locator("#admin-password").fill(ADMIN.password)
      await page.locator("#admin-confirmation").fill(`${ADMIN.password}-other`)
      await page.locator("#admin-submit").click()
      await expect(page.locator("#admin-error")).toHaveText("两次输入的密码不一致。")

      await page.locator("#admin-confirmation").fill(ADMIN.password)
      await page.locator("#admin-submit").click()
      await expect.poll(() => new URL(page.url()).pathname, { timeout: 120_000 }).toBe("/login")
      origin = new URL(page.url()).origin
      expect(origin).toMatch(/^http:\/\/127\.0\.0\.1:\d+$/)

      // The account the app created signs in to the workspace the stack serves.
      await page.locator("#login-username").fill(ADMIN.username)
      await page.locator("#login-password").fill(ADMIN.password)
      await page.locator("button[type=submit]").click()
      await expect.poll(() => new URL(page.url()).pathname, { timeout: 60_000 }).toBe("/paper-agent")

      const saved = JSON.parse(await readFile(path.join(userData, "host-preferences", "connection.json"), "utf8"))
      expect(saved).toMatchObject({ schema: "agora.desktop-connection/v2", mode: "local" })
      const env = await readFile(path.join(userData, "stack", ENV_FILE), "utf8")
      expect(env).toContain(`AGORA_IMAGE_TAG=${imageTag}`)
      expect(env).toContain(`AGORA_WEB_PORT=${new URL(origin).port}`)
    } finally {
      await quit(first.app)
    }

    // The containers outlive the app; the next launch finds the same stack and opens it directly.
    const second = await launch(userData, launchEnvironment)
    try {
      await expect.poll(() => second.page.url(), { timeout: 60_000 }).toMatch(new RegExp(`^${origin.replace(/\./g, "\\.")}/(login|paper-agent)`))
      await expect(second.page.locator("#admin-form")).toHaveCount(0)
    } finally {
      await quit(second.app)
    }
  })
})
