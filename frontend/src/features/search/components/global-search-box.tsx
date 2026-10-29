"use client"

import { SearchField } from "@/components/common/search-field"
import { useI18n } from "@/lib/i18n/use-i18n"

export function GlobalSearchBox({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  const { locale } = useI18n()
  const placeholder = locale === "zh" ? "搜索新闻、主题、证据、报告、技术、记忆..." : "Search news, topics, evidence, reports, technology, memory..."

  return (
    <SearchField
      className="max-w-2xl"
      inputClassName="h-9"
      placeholder={placeholder}
      aria-label={placeholder}
      value={value}
      onChange={(event) => onChange(event.target.value)}
    />
  )
}
