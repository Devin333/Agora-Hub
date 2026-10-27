import type { Config } from "tailwindcss"
import { typeScale } from "./src/styles/type-scale"

/**
 * Maps a design token from `src/styles/theme-tokens.css` to a Tailwind color.
 * A bare utility resolves to the token itself, so `bg-card` computes exactly to
 * `var(--card)`. Tokens may already carry alpha (ink-at-opacity colors), so an
 * opacity modifier mixes the token toward transparent instead of rewriting its
 * channels: `bg-border/50` is the border ink at half of its own opacity.
 */
function token(name: string) {
  return ({ opacityValue }: { opacityValue?: string }) =>
    opacityValue === undefined || opacityValue === "1"
      ? `var(--${name})`
      : `color-mix(in srgb, var(--${name}) calc(${opacityValue} * 100%), transparent)`
}

type TokenColor = ReturnType<typeof token>
interface TokenPalette { [key: string]: TokenColor | TokenPalette }

/**
 * Tailwind 3 resolves function colors at build time (it calls them with the
 * utility's `opacityValue`), but its config types only describe string colors.
 */
function themeColors(palette: TokenPalette) {
  return palette as unknown as Record<string, string>
}

/*
 * The legacy `*-opacity-*` utilities are off: they would feed every bare color
 * utility a `var(--tw-*-opacity)` alpha, and opacity is expressed with `/50`
 * modifiers instead.
 */
const disabledOpacityPlugins = {
  backgroundOpacity: false,
  borderOpacity: false,
  divideOpacity: false,
  placeholderOpacity: false,
  ringOpacity: false,
  textOpacity: false
} as const

const config: Config = {
  darkMode: ["variant", '&:where([data-theme="dark"], [data-theme="dark"] *)'],
  content: ["./src/**/*.{ts,tsx}"],
  corePlugins: disabledOpacityPlugins,
  theme: {
    extend: {
      colors: themeColors({
        background: token("background"),
        foreground: token("foreground"),
        dialog: token("dialog"),
        card: { DEFAULT: token("card"), foreground: token("card-foreground") },
        popover: { DEFAULT: token("popover"), foreground: token("popover-foreground") },
        primary: {
          DEFAULT: token("primary"),
          foreground: token("primary-foreground"),
          hover: token("primary-hover"),
          disabled: token("primary-disabled")
        },
        secondary: { DEFAULT: token("secondary"), foreground: token("secondary-foreground") },
        "chat-user": { DEFAULT: token("chat-user"), foreground: token("chat-user-foreground") },
        muted: { DEFAULT: token("muted"), foreground: token("muted-foreground") },
        "faint-foreground": token("faint-foreground"),
        accent: { DEFAULT: token("accent"), foreground: token("accent-foreground") },
        destructive: {
          DEFAULT: token("destructive"),
          fill: token("destructive-fill"),
          foreground: token("destructive-foreground"),
          text: token("destructive-text")
        },
        border: token("border"),
        input: token("input"),
        ring: token("ring"),
        sidebar: {
          DEFAULT: token("sidebar"),
          foreground: token("sidebar-foreground"),
          border: token("sidebar-border"),
          accent: token("sidebar-accent"),
          "accent-foreground": token("sidebar-accent-foreground")
        },
        link: token("link"),
        success: { DEFAULT: token("success"), text: token("success-text") },
        warning: { DEFAULT: token("warning"), text: token("warning-text") },
        "status-working": token("status-working"),
        unread: token("unread"),
        "working-dot": token("working-dot"),
        overlay: token("overlay"),
        scrollbar: { DEFAULT: token("scrollbar"), hover: token("scrollbar-hover") }
      }),
      borderRadius: {
        sm: "calc(var(--radius) - 4px)",
        md: "calc(var(--radius) - 2px)",
        lg: "var(--radius)",
        xl: "calc(var(--radius) + 4px)",
        "2xl": "calc(var(--radius) + 8px)"
      },
      fontFamily: {
        sans: [
          "-apple-system",
          "BlinkMacSystemFont",
          '"Segoe UI"',
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Microsoft YaHei UI"',
          '"Microsoft YaHei"',
          "sans-serif"
        ],
        serif: ["Georgia", '"Times New Roman"', '"Noto Serif CJK SC"', '"Source Han Serif SC"', '"Songti SC"', "serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "Monaco", "Consolas", '"Liberation Mono"', '"Courier New"', "monospace"]
      },
      fontSize: typeScale,
      ringWidth: {
        3: "3px"
      },
      keyframes: {
        "fade-in": { from: { opacity: "0" } },
        "fade-out": { to: { opacity: "0" } },
        "pop-in": { from: { opacity: "0", transform: "scale(0.95)" } },
        "pop-out": { to: { opacity: "0", transform: "scale(0.95)" } },
        "dialog-in": { from: { opacity: "0", transform: "translate(-50%, -50%) scale(0.95)" } },
        "dialog-out": { to: { opacity: "0", transform: "translate(-50%, -50%) scale(0.95)" } },
        "sheet-in-left": { from: { opacity: "0", transform: "translateX(-2.5rem)" } },
        "sheet-in-right": { from: { opacity: "0", transform: "translateX(2.5rem)" } }
      },
      animation: {
        "fade-in": "fade-in 100ms ease-out",
        "fade-out": "fade-out 100ms ease-in",
        "pop-in": "pop-in 100ms ease-out",
        "pop-out": "pop-out 100ms ease-in",
        "dialog-in": "dialog-in 100ms ease-out",
        "dialog-out": "dialog-out 100ms ease-in",
        "sheet-in-left": "sheet-in-left 200ms ease-out",
        "sheet-in-right": "sheet-in-right 200ms ease-out"
      }
    }
  },
  plugins: []
}

export default config
