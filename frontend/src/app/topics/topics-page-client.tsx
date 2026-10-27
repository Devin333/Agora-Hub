"use client"

import { useState } from "react"
import { PageHeader } from "@/components/layout/page-header"
import { TopicList } from "@/features/topics/components/topic-list"
import { TopicToolbar } from "@/features/topics/components/topic-toolbar"
import { useTopicList } from "@/features/topics/hooks/use-topic-list"
import { useI18n } from "@/lib/i18n/use-i18n"
import type { TopicFilters } from "@/types/topic"

export function TopicsPageClient() {
  const { t } = useI18n()
  const [filters, setFilters] = useState<TopicFilters>({ sort: "heatScore", viewMode: "grid" })
  const topics = useTopicList(filters)

  return (
    <div className="space-y-5">
      <PageHeader
        eyebrow={t("portal.topics.eyebrow")}
        title={t("portal.topics.title")}
        description={t("portal.topics.description")}
      />
      <TopicToolbar filters={filters} onChange={setFilters} />
      <TopicList topics={topics.data} viewMode={filters.viewMode ?? "grid"} />
    </div>
  )
}
