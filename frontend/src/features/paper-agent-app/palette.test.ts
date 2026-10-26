import { readFileSync } from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"
import postcss from "postcss"
import styles from "./paper-agent-app.module.css"

const read = (file: string) => readFileSync(path.join(process.cwd(), "src/features/paper-agent-app", file), "utf8")
const css = read("paper-agent-app.module.css")
const themeCss = read("paper-agent-theme.css")
const lightBlock = themeCss.match(/^\[data-paper-agent-shell\] \{([\s\S]*?)\n\}/m)?.[1] ?? ""
const darkBlock = themeCss.match(/^\[data-paper-agent-shell\]\[data-theme="dark"\] \{([\s\S]*?)\n\}/m)?.[1] ?? ""

function palette(block: string) {
  const value = (name: string) => block.match(new RegExp(`--${name}:\\s*(#[0-9a-f]{6})`, "i"))?.[1]
  return {
    danger: value("danger")!, text: value("text")!, bg: value("bg")!, surface: value("surface")!, surface2: value("surface-2")!, fill: value("fill")!,
    muted: value("muted")!, faint: value("faint")!, hover: value("hover")!, selected: value("selected")!, primaryHover: value("primary-hover")!,
  }
}
function luminance(hex: string) {
  const channels = [1, 3, 5].map(index => Number.parseInt(hex.slice(index, index + 2), 16) / 255).map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
  return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
}
function contrast(foreground: string, background: string) {
  const values = [luminance(foreground), luminance(background)].sort((a, b) => b - a)
  return (values[0] + 0.05) / (values[1] + 0.05)
}

describe("paper agent text palette", () => {
  it("defines every theme color on the grayscale axis and keeps component styles on tokens", () => {
    expect(lightBlock).not.toBe("")
    expect(darkBlock).not.toBe("")
    const hexColors = [...themeCss.matchAll(/#([0-9a-f]{6})\b/gi)].map(match => match[1])
    expect(hexColors.length).toBeGreaterThan(30)
    for (const value of hexColors) {
      expect(new Set([value.slice(0, 2), value.slice(2, 4), value.slice(4, 6)]).size, `non-monochrome theme color #${value}`).toBe(1)
    }
    for (const match of themeCss.matchAll(/rgba?\(\s*(\d+)[\s,]+(\d+)[\s,]+(\d+)/gi)) {
      expect(new Set(match.slice(1, 4)).size, `non-monochrome theme color ${match[0]}`).toBe(1)
    }
    for (const file of ["paper-agent-app.module.css", "paper-agent-settings-panel.module.css"]) {
      const source = read(file)
      expect(source, file).not.toMatch(/#[0-9a-f]{3,8}\b/i)
      expect(source, file).not.toMatch(/\b(?:hsla?|lab|lch|oklab|oklch|color-mix)\(/i)
    }
  })

  it("keeps muted, faint, and actual danger status text at WCAG AA contrast on their real surfaces", () => {
    expect(css).toContain(".errorText, .runtimeError { color: var(--danger); }")
    expect(css).toContain("--research-source-error-color: var(--muted)")
    for (const colors of [palette(lightBlock), palette(darkBlock)]) {
      expect(colors.danger).toBe(colors.muted)
      for (const foreground of [colors.muted, colors.faint, colors.danger]) {
        for (const background of [colors.bg, colors.surface, colors.surface2, colors.fill]) expect(contrast(foreground, background)).toBeGreaterThanOrEqual(4.5)
      }
    }
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
    expect(declarationsFor("[data-pdf-toolbar]")).toMatchObject({ color: "var(--text)", background: "var(--surface)", "border-color": "var(--border)" })
    for (const slot of ["search", "thumbnails", "selection-panel", "canvas-scroll", "thumbnail-viewport", "loading-overlay", "message", "failure", "text", "muted", "divider"]) {
      expect(declarationsFor('[data-pdf-' + slot + ']')).toBeDefined()
    }
    expect(declarationsFor('[data-pdf-control]:disabled')).toMatchObject({ color: "var(--muted)", background: "var(--surface-2)", opacity: "1", cursor: "not-allowed" })
    expect(declarationsFor('[data-pdf-control]:focus-visible')).toMatchObject({ outline: "2px solid var(--text)", "outline-offset": "2px", "box-shadow": "none" })
    expect(declarationsFor('[data-pdf-control][aria-current]')).toMatchObject({ background: "var(--selected)", "border-color": "var(--text)" })
    expect(declarationsFor('[data-pdf-control="input"]::placeholder')).toMatchObject({ color: "var(--muted)", opacity: "1" })
    expect(declarationsFor('[data-pdf-control="primary"]')).toMatchObject({ color: "var(--surface)", background: "var(--text)" })
    for (const rule of rules.filter(rule => rule.selector.includes("data-pdf-text-layer"))) {
      expect(rule.declarations.background).toMatch(/^rgb\(0 0 0 \/ /)
    }
  })

  it("keeps PDF text, disabled labels, control borders, and focus visible in both real theme palettes", () => {
    for (const block of [lightBlock, darkBlock]) {
      const colors = palette(block)
      for (const surface of [colors.bg, colors.surface, colors.surface2, colors.fill, colors.hover, colors.selected]) {
        expect(contrast(colors.text, surface)).toBeGreaterThanOrEqual(4.5)
        expect(contrast(colors.muted, surface)).toBeGreaterThanOrEqual(4.5)
        // The same neutral text ink is the focus outline; borders use muted ink.
        expect(contrast(colors.text, surface)).toBeGreaterThanOrEqual(3)
        expect(contrast(colors.muted, surface)).toBeGreaterThanOrEqual(3)
      }
      expect(contrast(colors.surface, colors.text)).toBeGreaterThanOrEqual(4.5)
      expect(contrast(colors.surface, colors.muted)).toBeGreaterThanOrEqual(4.5)
      // Inverted controls keep their label legible while hovered.
      expect(contrast(colors.surface, colors.primaryHover)).toBeGreaterThanOrEqual(4.5)
    }
  })

  it("uses neutral live material save-state styles in both themes without dropping failure emphasis", () => {
    // Tokens switch per theme, so one rule serves light and dark; a dark-only override would mean a hard-coded palette.
    expect(css).toContain('.materialItem > span[data-save-state="pending"] { color: var(--muted); }')
    expect(css).not.toContain('[data-theme="dark"] .materialItem')
    expect(css).toContain('.materialItem > span[data-save-state="failed"] { color: var(--text); font-weight: 650; }')
    for (const oldColor of ["#986b00", "#b42318", "#e6b94d", "#ff8a80"]) expect(css).not.toContain(oldColor)
  })
})

