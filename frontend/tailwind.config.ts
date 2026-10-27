import type { Config } from "tailwindcss"

/**
 * Maps a design token from `src/styles/theme-tokens.css` to a Tailwind color.
 * Tokens may already carry alpha (ink-at-opacity colors), so opacity modifiers
 * mix the token toward transparent instead of rewriting its channels:
 * `bg-border/50` is the border ink at half of its own opacity.
 */
function token(name: string) {
  return `color-mix(in srgb, var(--${name}) calc(<alpha-value> * 100%), transparent)`
}

const config: Config = {
  darkMode: ["variant", '&:where([data-theme="dark"], [data-theme="dark"] *)'],
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
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
      },
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
      fontSize: {
        caption: ["11px", { lineHeight: "16px", letterSpacing: "0.005em" }],
        label: ["12px", { lineHeight: "16px" }],
        body2: ["13px", { lineHeight: "18px" }],
        body1: ["14px", { lineHeight: "20px" }],
        heading3: ["17px", { lineHeight: "24px", letterSpacing: "-0.008em" }],
        heading2: ["22px", { lineHeight: "28px", letterSpacing: "-0.012em" }],
        heading1: ["26px", { lineHeight: "32px", letterSpacing: "-0.015em" }]
      },
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
