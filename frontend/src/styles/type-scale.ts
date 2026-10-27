/**
 * Rakazo type scale, shared by `tailwind.config.ts` (which generates the
 * `text-caption` … `text-heading1` utilities) and `cn()` (which must know these
 * are font sizes, not colors, so merging them with a `text-*` color keeps both).
 */
export const typeScale = {
  caption: ["11px", { lineHeight: "16px", letterSpacing: "0.005em" }],
  label: ["12px", { lineHeight: "16px" }],
  body2: ["13px", { lineHeight: "18px" }],
  body1: ["14px", { lineHeight: "20px" }],
  heading3: ["17px", { lineHeight: "24px", letterSpacing: "-0.008em" }],
  heading2: ["22px", { lineHeight: "28px", letterSpacing: "-0.012em" }],
  heading1: ["26px", { lineHeight: "32px", letterSpacing: "-0.015em" }]
} satisfies Record<string, [string, { lineHeight: string; letterSpacing?: string }]>

export const typeScaleNames = Object.keys(typeScale) as Array<keyof typeof typeScale>
