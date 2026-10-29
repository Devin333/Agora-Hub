"use client"

import { NativeSelect, NativeSelectOption } from "@/components/common/native-select"
import { SearchField } from "@/components/common/search-field"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { TopicFilters, TopicTrend } from "@/types/topic"

const trends: TopicTrend[] = ["rising", "stable", "falling"]

/** Rakazo filter row for the topic list: keyword + entity search and compact selects. */
export function TopicToolbar({ filters, onChange }: { filters: TopicFilters; onChange: (filters: TopicFilters) => void }) {
  const { locale } = useI18n()
  const zh = locale === "zh"
  const categories = zh
    ? ["开发者工具", "智能体运行时", "模型", "开源", "效率工具", "基础设施", "评估", "工作流"]
    : ["Developer Tools", "Agent Runtime", "Models", "Open Source", "Productivity Tools", "Infrastructure", "Evaluation", "Workflow"]
  const trendLabels: Record<TopicTrend, string> = {
    rising: zh ? "上升" : "Rising",
    stable: zh ? "稳定" : "Stable",
    falling: zh ? "下降" : "Falling"
  }
  const keywordPlaceholder = zh ? "搜索主题、实体、标签..." : "Search topics, entities, tags..."
  const entityPlaceholder = zh ? "按实体筛选，例如 OpenAI" : "Filter by entity, for example OpenAI"

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <SearchField
          className="min-w-[14rem] flex-1 md:max-w-[360px]"
          placeholder={keywordPlaceholder}
          aria-label={keywordPlaceholder}
          value={filters.keyword ?? ""}
          onChange={(event) => onChange({ ...filters, keyword: event.target.value })}
        />
        <SearchField
          className="min-w-[12rem] flex-1 md:max-w-[280px]"
          placeholder={entityPlaceholder}
          aria-label={entityPlaceholder}
          value={filters.entity ?? ""}
          onChange={(event) => onChange({ ...filters, entity: event.target.value })}
        />
        <div className="flex flex-wrap items-center gap-2 md:ml-auto">
          <NativeSelect
            aria-label={zh ? "趋势" : "Trend"}
            value={filters.trend?.[0] ?? ""}
            onChange={(event) => onChange({ ...filters, trend: event.target.value ? [event.target.value as TopicTrend] : [] })}
          >
            <NativeSelectOption value="">{zh ? "全部趋势" : "All trends"}</NativeSelectOption>
            {trends.map((trend) => (
              <NativeSelectOption key={trend} value={trend}>
                {trendLabels[trend]}
              </NativeSelectOption>
            ))}
          </NativeSelect>
          <NativeSelect
            aria-label={zh ? "分类" : "Category"}
            value={filters.category?.[0] ?? ""}
            onChange={(event) => onChange({ ...filters, category: event.target.value ? [event.target.value] : [] })}
          >
            <NativeSelectOption value="">{zh ? "全部分类" : "All categories"}</NativeSelectOption>
            {categories.map((category) => (
              <NativeSelectOption key={category} value={category}>
                {category}
              </NativeSelectOption>
            ))}
          </NativeSelect>
          <NativeSelect
            aria-label={zh ? "排序" : "Sort"}
            value={filters.sort ?? "heatScore"}
            onChange={(event) => onChange({ ...filters, sort: event.target.value as TopicFilters["sort"] })}
          >
            <NativeSelectOption value="heatScore">{zh ? "热度" : "Heat"}</NativeSelectOption>
            <NativeSelectOption value="lastSeenAt">{zh ? "更新时间" : "Updated time"}</NativeSelectOption>
            <NativeSelectOption value="itemCount">{zh ? "新闻数" : "News count"}</NativeSelectOption>
            <NativeSelectOption value="qualityScore">{zh ? "质量" : "Quality"}</NativeSelectOption>
          </NativeSelect>
          <NativeSelect
            aria-label={zh ? "视图" : "View"}
            value={filters.viewMode ?? "grid"}
            onChange={(event) => onChange({ ...filters, viewMode: event.target.value as TopicFilters["viewMode"] })}
          >
            <NativeSelectOption value="grid">{zh ? "网格" : "Grid"}</NativeSelectOption>
            <NativeSelectOption value="list">{zh ? "列表" : "List"}</NativeSelectOption>
            <NativeSelectOption value="dense">{zh ? "紧凑" : "Dense"}</NativeSelectOption>
          </NativeSelect>
        </div>
      </div>
    </div>
  )
}
