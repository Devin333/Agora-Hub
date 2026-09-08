import { type CSSProperties } from "react"

// Provider brand assets, not recolored UI glyphs.
export function ProviderIcon({ provider }: { provider: "google" | "wechat" | "qq" }) {
  const style = { width: 24, height: 24, display: "block" } satisfies CSSProperties
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={`/auth/${provider}.svg`} style={style} alt="" aria-hidden="true" />
}
