import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"
import { generateStackSecrets, mergeStackEnv, parseEnvFile, parsePort, renderEnvFile, templateKeys } from "../src/local-stack-env"

const TEMPLATE = readFileSync(path.resolve(__dirname, "../../deploy/desktop/env.template"), "utf8")
const settings = { AGORA_IMAGE_REGISTRY: "ghcr.io/devin333", AGORA_IMAGE_TAG: "v0.1.2" }

function bytes(seed: number) {
  return (size: number) => Buffer.alloc(size, seed)
}

describe("local stack secrets", () => {
  it("uses the encodings the server checks", () => {
    // 0xfb bytes produce both characters that differ between base64 and its URL-safe form.
    const secrets = generateStackSecrets(bytes(0xfb))
    const credential = Buffer.from(secrets.NEWSROOM_CREDENTIAL_ENCRYPTION_KEY, "base64")
    expect(credential).toHaveLength(32)
    expect(secrets.NEWSROOM_CREDENTIAL_ENCRYPTION_KEY).toMatch(/^[A-Za-z0-9+/]{43}=$/)
    // A Fernet key: URL-safe base64 of 32 bytes, padding kept.
    expect(secrets.NEWS_ACTIVITY_ENCRYPTION_KEY).toMatch(/^[A-Za-z0-9_-]{43}=$/)
    expect(Buffer.from(secrets.NEWS_ACTIVITY_ENCRYPTION_KEY, "base64url")).toHaveLength(32)
    expect(secrets.AGORA_DESKTOP_STACK_TOKEN).toMatch(/^[A-Za-z0-9_-]{43}$/)
  })

  it("generates different secrets each time by default", () => {
    expect(generateStackSecrets().AGORA_DESKTOP_STACK_TOKEN).not.toBe(generateStackSecrets().AGORA_DESKTOP_STACK_TOKEN)
  })
})

describe("local stack env file", () => {
  it("fills in every key the packaged template declares", () => {
    const env = mergeStackEnv(new Map(), { ...settings, AGORA_WEB_PORT: "45180" }, bytes(7))
    const rendered = renderEnvFile(TEMPLATE, env)
    const values = parseEnvFile(rendered)
    expect([...values.keys()].sort()).toEqual(templateKeys(TEMPLATE).sort())
    expect(values.get("AGORA_WEB_PORT")).toBe("45180")
    expect(values.get("AGORA_IMAGE_TAG")).toBe("v0.1.2")
    for (const value of values.values()) expect(value).not.toBe("")
    // The template's explanations stay with the values.
    expect(rendered).toContain("# Loopback port")
    expect(rendered.endsWith("\n")).toBe(true)
  })

  it("keeps the secrets and the port of an existing file and updates the release", () => {
    const first = mergeStackEnv(new Map(), { ...settings, AGORA_WEB_PORT: "45181" }, bytes(1))
    const existing = parseEnvFile(renderEnvFile(TEMPLATE, first))
    const second = mergeStackEnv(existing, { AGORA_IMAGE_REGISTRY: "registry.cn-hangzhou.aliyuncs.com/agora-hub", AGORA_IMAGE_TAG: "v0.1.3" }, bytes(2))
    expect(second).toMatchObject({
      NEWSROOM_CREDENTIAL_ENCRYPTION_KEY: first.NEWSROOM_CREDENTIAL_ENCRYPTION_KEY,
      NEWS_ACTIVITY_ENCRYPTION_KEY: first.NEWS_ACTIVITY_ENCRYPTION_KEY,
      AGORA_DESKTOP_STACK_TOKEN: first.AGORA_DESKTOP_STACK_TOKEN,
      AGORA_WEB_PORT: "45181",
      AGORA_IMAGE_REGISTRY: "registry.cn-hangzhou.aliyuncs.com/agora-hub",
      AGORA_IMAGE_TAG: "v0.1.3"
    })
  })

  it("refuses a template that disagrees with the app, and unsafe values", () => {
    const env = mergeStackEnv(new Map(), { ...settings, AGORA_WEB_PORT: "45180" }, bytes(3))
    expect(() => renderEnvFile(`${TEMPLATE}EXTRA_KEY=\n`, env)).toThrow("unknown key EXTRA_KEY")
    expect(() => renderEnvFile(TEMPLATE.replace(/^AGORA_WEB_PORT=.*$/m, ""), env)).toThrow("lacks key AGORA_WEB_PORT")
    expect(() => renderEnvFile(TEMPLATE, { ...env, AGORA_IMAGE_TAG: "v1\nNEWSROOM_DEPLOYMENT_PROFILE=default" })).toThrow("unsafe")
    expect(() => renderEnvFile(TEMPLATE, { ...env, AGORA_WEB_PORT: "" })).toThrow("unsafe")
  })

  it("accepts only unprivileged ports", () => {
    expect(parsePort("45180")).toBe(45180)
    expect(parsePort("80")).toBeNull()
    expect(parsePort("70000")).toBeNull()
    expect(parsePort("4518a")).toBeNull()
    expect(parsePort(undefined)).toBeNull()
  })
})
