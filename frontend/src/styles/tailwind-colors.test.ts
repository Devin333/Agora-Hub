import { describe, expect, it } from "vitest"
import postcss from "postcss"
import tailwindcss from "tailwindcss"
import config from "../../tailwind.config"

async function utilities(classes: string) {
  const result = await postcss([tailwindcss({ ...config, content: [{ raw: `<div class="${classes}"></div>`, extension: "html" }] })])
    .process("@tailwind utilities;", { from: undefined })
  return result.css
}

describe("tailwind token colors", () => {
  it("resolves a bare color utility to the token itself, so computed colors match the token", async () => {
    const css = await utilities("bg-card text-success-text border-border ring-ring")
    expect(css).toMatch(/\.bg-card \{\s*background-color: var\(--card\)\s*\}/)
    expect(css).toMatch(/\.text-success-text \{\s*color: var\(--success-text\)\s*\}/)
    expect(css).toMatch(/\.border-border \{\s*border-color: var\(--border\)\s*\}/)
    expect(css).toMatch(/--tw-ring-color: var\(--ring\)/)
    expect(css).not.toContain("--tw-bg-opacity")
    expect(css).not.toContain("--tw-text-opacity")
  })

  it("mixes the token toward transparent for an opacity modifier", async () => {
    const css = await utilities("bg-border/50 text-foreground/70")
    expect(css).toContain("background-color: color-mix(in srgb, var(--border) calc(0.5 * 100%), transparent)")
    expect(css).toContain("color: color-mix(in srgb, var(--foreground) calc(0.7 * 100%), transparent)")
  })
})
