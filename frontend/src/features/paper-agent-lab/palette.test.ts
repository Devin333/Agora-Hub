import { readFileSync } from "node:fs"
import path from "node:path"
import postcss from "postcss"
import { describe, expect, it } from "vitest"
import assistantStyles from "./assistant-panel.module.css"
import libraryStyles from "./library-panel.module.css"
import labStyles from "./paper-agent-lab.module.css"
import readerStyles from "./reader-panel.module.css"
import sidebarStyles from "./workspace-sidebar.module.css"

const read = (file: string) => readFileSync(path.join(process.cwd(), "src", file), "utf8")
const modules = ["paper-agent-lab", "assistant-panel", "library-panel", "reader-panel", "workspace-sidebar"] as const
const sources = Object.fromEntries(
  modules.map((name) => [name, read(`features/paper-agent-lab/${name}.module.css`)]),
) as Record<(typeof modules)[number], string>
const tokensCss = read("styles/theme-tokens.css")
const globalsCss = read("styles/globals.css")
const definedTokens = new Set(
  [...`${tokensCss}\n${globalsCss}`.matchAll(/^\s*--([a-z0-9-]+):/gm)].map((match) => match[1]),
)

/** Variables a stylesheet defines for its own children (module-local layout hooks, not global tokens). */
function locallyDefined(source: string) {
  return new Set([...source.matchAll(/(?:^|[\s;{])--([a-z0-9-]+):/g)].map((match) => match[1]))
}

function withoutComments(source: string) {
  return source.replace(/\/\*[\s\S]*?\*\//g, "")
}

/** Every declaration of `property` in rules whose selector contains `selector`. */
function declarations(source: string, selector: string, property: string) {
  const values: string[] = []
  postcss.parse(source).walkRules((rule) => {
    if (!rule.selector.split(",").some((part) => part.trim() === selector)) return
    rule.walkDecls(property, (declaration) => { values.push(declaration.value) })
  })
  return values
}

describe("paper agent lab palette", () => {
  it("compiles every module", () => {
    // Importing the real modules exercises CSS-module syntax/compilation.
    expect(labStyles.root).toBeTruthy()
    expect(assistantStyles.panel).toBeTruthy()
    expect(libraryStyles.panel).toBeTruthy()
    expect(readerStyles.reader).toBeTruthy()
    expect(sidebarStyles.sidebar).toBeTruthy()
  })

  it.each(modules)("styles %s only through the global design tokens", (name) => {
    const source = sources[name]
    const local = locallyDefined(source)
    for (const match of source.matchAll(/var\(--([a-z0-9-]+)/g)) {
      expect(definedTokens.has(match[1]) || local.has(match[1]), `${name} uses undefined token --${match[1]}`).toBe(true)
    }
    expect(source).not.toMatch(/#[0-9a-f]{3,8}\b/i)
    expect(source).not.toMatch(/\b(?:rgba?|hsla?|lab|lch|oklab|oklch)\(/i)
    // Local custom properties are layout hooks only: none may carry a color.
    for (const [, property, value] of source.matchAll(/(?:^|[\s;{])--([a-z0-9-]+):\s*([^;]+);/g)) {
      expect(value, `${name} --${property}`).toMatch(/^-?\d+(?:\.\d+)?px$/)
    }
  })

  it.each(modules)("keeps no private palette or local theme switch in %s", (name) => {
    const source = withoutComments(sources[name])
    expect(source).not.toMatch(/--(?:lab|pa)-/)
    expect(source).not.toMatch(/data-theme/)
    expect(source).not.toMatch(/prefers-color-scheme/)
    expect(source).not.toMatch(/color-scheme\s*:/)
  })

  it("draws no custom font stacks beyond the shared serif and mono tokens", () => {
    for (const [name, source] of Object.entries(sources)) {
      for (const [, value] of source.matchAll(/font-family:\s*([^;]+);/g)) {
        expect(value, name).toMatch(/^(?:inherit|theme\("fontFamily\.(?:serif|mono)"\))$/)
      }
    }
  })

  it("lays the workspace out on the Rakazo columns", () => {
    const lab = sources["paper-agent-lab"]
    expect(declarations(lab, ".workspace", "grid-template-columns")).toContain("232px 316px minmax(0, 1fr)")
    expect(declarations(lab, ".root[data-assistant] .workspace", "grid-template-columns")).toContain("232px minmax(0, 1fr) 384px")
    expect(declarations(sources["workspace-sidebar"], ".sidebar", "background")).toEqual(["var(--sidebar)"])
    expect(declarations(sources["library-panel"], ".panel", "background")).toEqual(["var(--sidebar)"])
    expect(declarations(sources["library-panel"], ".paperItem[data-selected]", "background")).toEqual(["var(--sidebar-accent)"])
    expect(declarations(sources["library-panel"], ".saveButton", "border-radius")).toEqual(["999px"])
  })

  it("floats the reader controls as pills over the scrolling document", () => {
    const reader = sources["reader-panel"]
    expect(declarations(reader, ".toolbar", "position")).toEqual(["absolute"])
    expect(declarations(reader, ".toolbar", "pointer-events")).toEqual(["none"])
    expect(declarations(reader, ".scroller", "padding-top")).toEqual(["var(--reader-pills-offset)"])
    expect(declarations(reader, ".actionButton", "border-radius")).toEqual(["999px"])
    expect(declarations(reader, ".actionButton", "width")).toEqual(["36px"])
    expect(declarations(reader, ".tabs", "border-radius")).toEqual(["999px"])
  })

  it("renders the assistant as chat bubbles above a rounded composer", () => {
    const assistant = sources["assistant-panel"]
    expect(declarations(assistant, ".question", "background")).toEqual(["var(--chat-user)"])
    expect(declarations(assistant, ".question", "color")).toEqual(["var(--chat-user-foreground)"])
    expect(declarations(assistant, ".question", "border-radius")).toEqual(["20px"])
    expect(declarations(assistant, ".previewNotice", "background")).toEqual(["var(--muted)"])
    expect(declarations(assistant, ".previewNotice", "border-radius")).toEqual(["20px"])
    expect(declarations(assistant, ".inputShell", "border-radius")).toEqual(["26px"])
    expect(declarations(assistant, ".inputShell button", "background")).toEqual(["var(--primary)"])
    expect(declarations(assistant, ".inputShell button", "border-radius")).toEqual(["999px"])
  })

  it("keeps every border a hairline and every radius on the 12/16/20 scale or a pill", () => {
    const allowedRadii = new Set(["3px", "6px", "8px", "9px", "10px", "12px", "16px", "20px", "26px", "999px"])
    for (const [name, source] of Object.entries(sources)) {
      postcss.parse(source).walkDecls((declaration) => {
        if (/^border(?:-(?:top|right|bottom|left))?$/.test(declaration.prop) && declaration.value !== "0") {
          // `transparent` reserves the hairline a field paints with --ring on focus.
          expect(declaration.value, `${name} ${declaration.prop}`).toMatch(/^1px solid (?:var\(--[a-z-]+\)|transparent)$/)
        }
        if (declaration.prop === "border-radius") {
          expect(allowedRadii.has(declaration.value), `${name} radius ${declaration.value}`).toBe(true)
        }
      })
    }
  })
})
