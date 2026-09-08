import { describe, expect, it } from "vitest"
import { preserveEntryParams } from "@/app/community/community-page-client"

describe("CommunityPageClient research context", () => {
  it("preserves the homepage question and source while filters change", () => {
    const current = new URLSearchParams("question=Agent+memory&q=memory&entry=home&source=reddit&signal=topic-1")
    const next = preserveEntryParams(current, new URLSearchParams("q=latency&period=weekly&source=hackernews"))

    expect(next.get("question")).toBe("Agent memory")
    expect(next.get("entry")).toBe("home")
    expect(next.get("source")).toBe("hackernews")
    expect(next.get("q")).toBe("latency")
    expect(next.get("period")).toBe("weekly")
  })

  it("does not retain a selected signal that the caller did not add", () => {
    const current = new URLSearchParams("question=Agent+memory&source=home&signal=topic-1")
    const next = preserveEntryParams(current, new URLSearchParams("q=memory"))

    expect(next.has("signal")).toBe(false)
  })
})
