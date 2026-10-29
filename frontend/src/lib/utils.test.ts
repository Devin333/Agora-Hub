import { describe, expect, it } from "vitest"
import { typeScaleNames } from "@/styles/type-scale"
import { cn } from "./utils"

describe("cn", () => {
  it("keeps a custom type-scale size next to a text color", () => {
    for (const name of typeScaleNames) {
      expect(cn(`text-${name}`, "text-muted-foreground")).toBe(`text-${name} text-muted-foreground`)
      expect(cn("text-success-text", `text-${name}`)).toBe(`text-success-text text-${name}`)
    }
  })

  it("lets a later size win over an earlier one, across built-in and custom sizes", () => {
    expect(cn("text-sm", "text-body2")).toBe("text-body2")
    expect(cn("text-heading2", "text-lg")).toBe("text-lg")
    expect(cn("text-label", "text-caption")).toBe("text-caption")
  })
})
