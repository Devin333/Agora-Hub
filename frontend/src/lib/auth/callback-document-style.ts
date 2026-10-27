import { THEME_COLORS } from "@/lib/theme/appearance"

/**
 * Inline stylesheet for the standalone OAuth completion document. It is served
 * under a nonce-only CSP without the app's CSS, so it repeats the Rakazo
 * background/foreground/link values of `src/styles/theme-tokens.css` and follows
 * the platform color scheme (the popup has no access to the stored preference).
 */
export const CALLBACK_DOCUMENT_STYLE = [
  `:root{color-scheme:light dark;--background:${THEME_COLORS.light};--foreground:#1c1c1c;--muted-foreground:#1c1c1cad;--link:#0b6dd3}`,
  `@media (prefers-color-scheme:dark){:root{--background:${THEME_COLORS.dark};--foreground:#f5f5f5;--muted-foreground:#f5f5f5a8;--link:#4d9bff}}`,
  "body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:var(--background);color:var(--foreground);font:14px/1.6 ui-sans-serif,system-ui,-apple-system,\"Segoe UI\",\"PingFang SC\",\"Microsoft YaHei UI\",sans-serif;text-align:center;-webkit-font-smoothing:antialiased}",
  "h1{margin:0 0 6px;font-size:22px;font-weight:600;letter-spacing:-.012em}",
  "p{margin:0;color:var(--muted-foreground)}",
  "a{display:inline-block;margin-top:20px;color:var(--link);font-weight:500;text-decoration:none}a:hover{text-decoration:underline}",
].join("")
