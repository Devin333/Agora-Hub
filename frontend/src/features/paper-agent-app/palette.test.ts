import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"
import postcss from "postcss"
import styles from "./paper-agent-app.module.css"

const read = (file: string) => readFileSync(path.join(process.cwd(), "src", file), "utf8")
const css = read("features/paper-agent-app/paper-agent-app.module.css")
const settingsCss = read("features/paper-agent-app/paper-agent-settings-panel.module.css")
const tokensCss = read("styles/theme-tokens.css")

type Rgba = [number, number, number, number]

function tokenBlock(selector: string) {
  const escaped = selector.replace(/[[\]"]/g, match => `\\${match}`)
  return tokensCss.match(new RegExp(`${escaped} \\{([\\s\\S]*?)\\n\\}`))?.[1] ?? ""
}
const themes = {
  dark: tokenBlock(':root,\n[data-theme="dark"]'),
  light: tokenBlock('[data-theme="light"]'),
}
const definedTokens = new Set([...tokensCss.matchAll(/^\s*--([a-z0-9-]+):/gm)].map(match => match[1]))

function parseHex(hex: string): Rgba {
  const value = hex.slice(1)
  const channel = (index: number) => Number.parseInt(value.slice(index, index + 2), 16)
  return [channel(0), channel(2), channel(4), value.length === 8 ? channel(6) / 255 : 1]
}
function token(block: string, name: string): Rgba {
  const value = block.match(new RegExp(`--${name}:\\s*(#[0-9a-f]{6,8})\\b`, "i"))?.[1]
  if (!value) throw new Error(`missing token --${name}`)
  return parseHex(value)
}
/** Resolve a derived `color-mix(in srgb, var(--a) N%, var(--b))` status ink defined after the theme blocks. */
function derived(block: string, name: string): Rgba {
  const recipe = tokensCss.match(new RegExp(`--${name}: color-mix\\(in srgb, var\\(--([a-z-]+)\\) (\\d+)%, var\\(--([a-z-]+)\\)\\);`))
  if (!recipe) throw new Error(`missing derived token --${name}`)
  const [first, second, share] = [token(block, recipe[1]), token(block, recipe[3]), Number(recipe[2]) / 100]
  return [0, 1, 2].map(index => first[index] * share + second[index] * (1 - share)).concat(1) as Rgba
}
/** Composite a translucent ink over an opaque surface, as the browser paints it. */
function over([r, g, b, a]: Rgba, [br, bg, bb]: Rgba): Rgba {
  return [r * a + br * (1 - a), g * a + bg * (1 - a), b * a + bb * (1 - a), 1]
}
function luminance([r, g, b]: Rgba) {
  const linear = [r, g, b].map(value => value / 255).map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
}
function contrast(foreground: Rgba, background: Rgba) {
  const painted = over(foreground, background)
  const values = [luminance(painted), luminance(background)].sort((a, b) => b - a)
  return (values[0] + 0.05) / (values[1] + 0.05)
}

/** Variables a stylesheet defines for its own children (module-local hooks, not global tokens). */
function locallyDefined(source: string) {
  return new Set([...source.matchAll(/(?:^|[\s;{])--([a-z0-9-]+):/g)].map(match => match[1]))
}

describe("paper agent palette", () => {
  it("styles the workbench and settings dialog only through the global design tokens", () => {
    for (const [file, source] of [["paper-agent-app.module.css", css], ["paper-agent-settings-panel.module.css", settingsCss]] as const) {
      const local = locallyDefined(source)
      for (const match of source.matchAll(/var\(--([a-z0-9-]+)/g)) {
        expect(definedTokens.has(match[1]) || local.has(match[1]), `${file} uses undefined token --${match[1]}`).toBe(true)
      }
      expect(source, file).not.toMatch(/#[0-9a-f]{3,8}\b/i)
      expect(source, file).not.toMatch(/\b(?:hsla?|lab|lch|oklab|oklch)\(/i)
    }
    // Only the PDF text layer keeps literal ink: it paints over the always-white rendered page.
    const literalRgb = css.split("\n").filter(line => /\brgba?\(/.test(line))
    expect(literalRgb.length).toBeGreaterThan(0)
    for (const line of literalRgb) expect(line).toContain("[data-pdf-text-layer]")
  })

  it("keeps secondary text and every status color at WCAG AA contrast on the surfaces they sit on", () => {
    for (const [name, block] of Object.entries(themes)) {
      expect(block, name).not.toBe("")
      const surfaces = ["background", "card", "sidebar", "dialog"].map(surface => token(block, surface))
      for (const ink of ["foreground", "muted-foreground", "link"]) {
        for (const surface of surfaces) expect(contrast(token(block, ink), surface), `${name} --${ink}`).toBeGreaterThanOrEqual(4.5)
      }
      for (const status of ["success", "warning", "destructive"]) {
        // Raw hues are for icons and dots (non-text, 3:1); status text uses the derived ink, also on its own tint.
        const hue = token(block, status)
        const text = derived(block, `${status}-text`)
        for (const surface of surfaces) {
          expect(contrast(hue, surface), `${name} --${status} icon`).toBeGreaterThanOrEqual(3)
          expect(contrast(text, surface), `${name} --${status}-text`).toBeGreaterThanOrEqual(4.5)
          expect(contrast(text, over([hue[0], hue[1], hue[2], .15], surface)), `${name} --${status}-text on tint`).toBeGreaterThanOrEqual(4.5)
        }
      }
      // Primary buttons and the selected sidebar row stay legible, including while hovered.
      expect(contrast(token(block, "primary-foreground"), token(block, "primary")), `${name} primary`).toBeGreaterThanOrEqual(4.5)
      expect(contrast(token(block, "primary-foreground"), token(block, "primary-hover")), `${name} primary hover`).toBeGreaterThanOrEqual(4.5)
      const selectedRow = over(token(block, "sidebar-accent"), token(block, "sidebar"))
      expect(contrast(token(block, "sidebar-accent-foreground"), selectedRow), `${name} selected row`).toBeGreaterThanOrEqual(4.5)
    }
  })

  it("colors real statuses with their status tokens", () => {
    expect(css).toContain('.materialItem > span[data-save-state="pending"], .materialState span[data-save-state="pending"] { color: var(--warning-text); }')
    expect(css).toContain('.materialItem > span[data-save-state="failed"] { color: var(--destructive-text); font-weight: 600; }')
    expect(css).toContain("--research-source-error-color: var(--destructive-text)")
    expect(css).toMatch(/\.connectionState\[data-online\] svg \{ color: var\(--success\); \}/)
    expect(css).toMatch(/\.errorText, \.runtimeError \{ color: var\(--destructive-text\); \}/)
  })

  it("scopes every PDF override to the opt-in reader, with explicit slots and no document filtering", () => {
    // Importing the real module also exercises CSS-module syntax/compilation.
    expect(styles.pdfViewer).toBeTruthy()
    const rules: Array<{ selector: string; declarations: Record<string, string> }> = []
    postcss.parse(css).walkRules(rule => {
      if (!rule.selector.includes("data-pdf-")) return
      const declarations: Record<string, string> = {}
      rule.walkDecls(declaration => { declarations[declaration.prop] = declaration.value })
      rules.push({ selector: rule.selector, declarations })
    })
    expect(rules.length).toBeGreaterThan(15)
    const scope = '.app .pdfViewer[data-pdf-appearance="paper-agent"]'
    for (const rule of rules) {
      for (const selector of rule.selector.split(",")) {
        expect(selector.trim().startsWith(scope)).toBe(true)
        expect(selector).not.toMatch(/\[class|\*|data-pdf-document/)
        expect(selector.replace(/\[[^\]]+\]/g, "")).not.toMatch(/\b(?:canvas|svg|img|div)\b/)
      }
      expect(rule.declarations).not.toHaveProperty("filter")
      expect(rule.declarations).not.toHaveProperty("backdrop-filter")
      for (const property of Object.keys(rule.declarations)) {
        if (property.startsWith("--")) expect(property).toBe("--tw-ring-color")
      }
    }
    const declarationsFor = (slot: string) => rules.find(rule => rule.selector.includes(slot))?.declarations
    expect(declarationsFor("[data-pdf-toolbar]")).toMatchObject({ color: "var(--foreground)", background: "var(--card)", "border-color": "var(--border)" })
    for (const slot of ["search", "thumbnails", "selection-panel", "canvas-scroll", "thumbnail-viewport", "loading-overlay", "message", "failure", "text", "muted", "divider"]) {
      expect(declarationsFor("[data-pdf-" + slot + "]")).toBeDefined()
    }
    expect(declarationsFor("[data-pdf-control]:disabled")).toMatchObject({ color: "var(--muted-foreground)", background: "var(--muted)", opacity: "1", cursor: "not-allowed" })
    expect(declarationsFor("[data-pdf-control]:focus-visible")).toMatchObject({ outline: "2px solid var(--foreground)", "outline-offset": "2px", "box-shadow": "none" })
    expect(declarationsFor("[data-pdf-control][aria-current]")).toMatchObject({ background: "var(--sidebar-accent)", "border-color": "var(--foreground)" })
    expect(declarationsFor('[data-pdf-control="input"]::placeholder')).toMatchObject({ color: "var(--muted-foreground)", opacity: "1" })
    expect(declarationsFor('[data-pdf-control="primary"]')).toMatchObject({ color: "var(--primary-foreground)", background: "var(--primary)" })
    for (const rule of rules.filter(rule => rule.selector.includes("data-pdf-text-layer"))) {
      expect(rule.declarations.background).toMatch(/^rgb\(0 0 0 \/ /)
    }
  })
})
