"use client"

import { Filter } from "lucide-react"
import { NativeSelect, NativeSelectOption } from "@/components/common/native-select"
import { SearchField } from "@/components/common/search-field"
import { Button } from "@/components/ui/button"
import { NewsViewModeToggle } from "@/features/news/components/news-view-mode-toggle"
import type { NewsFilterOptions, NewsFilters, NewsViewMode } from "@/types/news"

export function NewsToolbar({
  filters,
  options,
  onChange,
  onToggleFilters,
}: {
  filters: NewsFilters
  options: NewsFilterOptions
  onChange: (patch: Partial<NewsFilters>) => void
  onToggleFilters: () => void
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <SearchField
        className="min-w-[14rem] flex-1 md:max-w-[360px]"
        value={filters.keyword ?? ""}
        onChange={(event) => onChange({ keyword: event.target.value })}
        placeholder="Search news, topics, sources, tags"
        aria-label="Search news"
      />

      <div className="flex flex-wrap items-center gap-2 md:ml-auto">
        <NativeSelect
          value={filters.sourceType?.[0] ?? ""}
          onChange={(event) => onChange({ sourceType: event.target.value ? [event.target.value as NonNullable<NewsFilters["sourceType"]>[number]] : undefined })}
          aria-label="Source type"
        >
          <NativeSelectOption value="">All sources</NativeSelectOption>
          {options.sourceTypes.map((sourceType) => (
            <NativeSelectOption key={sourceType} value={sourceType}>
              {sourceTypeLabel(sourceType)}
            </NativeSelectOption>
          ))}
        </NativeSelect>

        <NativeSelect
          value={filters.sort ?? "publishedAt"}
          onChange={(event) => onChange({ sort: event.target.value as NewsFilters["sort"] })}
          aria-label="Sort"
        >
          <NativeSelectOption value="heatScore">Top</NativeSelectOption>
          <NativeSelectOption value="publishedAt">Newest</NativeSelectOption>
          <NativeSelectOption value="collectedAt">Recently collected</NativeSelectOption>
          <NativeSelectOption value="qualityScore">Trusted</NativeSelectOption>
        </NativeSelect>

        <NewsViewModeToggle value={(filters.viewMode ?? "card") as NewsViewMode} onChange={(viewMode) => onChange({ viewMode })} />

        <Button type="button" variant="outline" size="sm" onClick={onToggleFilters} className="xl:hidden">
          <Filter strokeWidth={1.9} />
          Filters
        </Button>
      </div>
    </div>
  )
}

function sourceTypeLabel(value: string) {
  const labels: Record<string, string> = {
    official_blog: "Official blog",
    rss: "RSS",
    atom: "Atom",
    github: "GitHub",
    hackernews: "Hacker News",
    reddit: "Reddit",
    arxiv: "arXiv",
    lobsters: "Lobsters",
    stackoverflow: "Stack Overflow",
    devto: "dev.to",
    medium: "Medium",
    html: "HTML",
    web_page: "Web page",
    media: "Media",
    manual: "Manual",
    custom: "Custom",
  }
  return labels[value] ?? value
}
