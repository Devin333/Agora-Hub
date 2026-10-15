import { describe, expect, it } from "vitest";
import { newsItems, topics } from "@/lib/mock-data";
import { applyNewsFilters, filtersFromSearchParams, filtersToSearchParams } from "@/features/news/lib/news-filters";

describe("reader news data", () => {
  it("meets the Task 02 mock data floor", () => {
    expect(newsItems.length).toBeGreaterThanOrEqual(12);
    expect(topics.length).toBeGreaterThanOrEqual(5);
    expect(newsItems.filter((item) => item.keyFacts?.length).length).toBeGreaterThanOrEqual(3);
    expect(newsItems.filter((item) => item.evidenceIds?.length).length).toBeGreaterThanOrEqual(3);
    expect(newsItems.filter((item) => item.topicId).length).toBeGreaterThanOrEqual(2);
    expect(newsItems.filter((item) => item.reportIds?.length).length).toBeGreaterThanOrEqual(1);
  });

  it("preserves URL-backed filter state", () => {
    const filters = filtersFromSearchParams(new URLSearchParams("q=agent&dateRange=today&sourceType=github,rss&credibility=high&topic=agents&view=table&sort=heatScore&page=2"));
    const params = filtersToSearchParams(filters);

    expect(filters.keyword).toBe("agent");
    expect(filters.dateRange).toBe("today");
    expect(filters.sourceType).toEqual(["github", "rss"]);
    expect(filters.topic).toBe("agents");
    expect(filters.credibility).toEqual(["high"]);
    expect(filters.viewMode).toBe("table");
    expect(filters.sort).toBe("heatScore");
    expect(filters.page).toBe(2);
    expect(params.toString()).toContain("dateRange=today");
    expect(params.toString()).toContain("topic=agents");
    expect(params.toString()).toContain("view=table");
    expect(params.toString()).toContain("sort=heatScore");
  });

  it("accepts PRD-style query aliases", () => {
    const filters = filtersFromSearchParams(new URLSearchParams("period=monthly&source=github&sort=newest"));

    expect(filters.dateRange).toBe("month");
    expect(filters.sourceType).toEqual(["github"]);
    expect(filters.sort).toBe("publishedAt");
  });

  it("filters and sorts news by heat score", () => {
    const items = applyNewsFilters(newsItems, { keyword: "agent", sort: "heatScore" });

    expect(items.length).toBeGreaterThan(0);
    expect(items[0].heatScore).toBeGreaterThanOrEqual(items[items.length - 1].heatScore);
    expect(items.some((item) => item.title.toLowerCase().includes("agent") || item.summary.toLowerCase().includes("agent"))).toBe(true);
  });

  it("does not treat missing scores as zero when sorting or filtering", () => {
    const items = applyNewsFilters(
      [
        { ...newsItems[0], id: "missing-score", heatScore: undefined, qualityScore: undefined },
        { ...newsItems[1], id: "scored", heatScore: 12, qualityScore: 82 },
      ],
      { sort: "heatScore" }
    );

    expect(items[0].id).toBe("scored");
    expect(applyNewsFilters(items, { qualityStatus: ["passed"] })).toHaveLength(1);
  });
});
