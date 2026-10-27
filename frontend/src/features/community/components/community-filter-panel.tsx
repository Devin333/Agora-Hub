import { NativeSelect, NativeSelectOption } from "@/components/common/native-select"
import { Button } from "@/components/ui/button"
import {
  COMMUNITY_SENTIMENTS,
  COMMUNITY_SORTS,
  COMMUNITY_SOURCE_TYPES,
  COMMUNITY_TOPIC_KEYS,
  communitySentimentLabel,
  communitySourceLabel,
  communityTopicLabel
} from "@/lib/community/community-filters"
import type {
  CommunityListParams,
  CommunitySort,
  CommunitySourceType,
  CommunityTopicKey,
  CommunityTopicSentiment
} from "@/types/community"

export function CommunityFilterPanel({
  filters,
  onChange
}: {
  filters: CommunityListParams
  onChange: (patch: Partial<CommunityListParams>) => void
}) {
  return (
    <aside aria-label="Community filters" className="rounded-2xl border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-[15px] font-semibold text-foreground">Filters</h2>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-[12px]"
          onClick={() =>
            onChange({
              source: undefined,
              sentiment: undefined,
              topic: undefined,
              sort: "trending"
            })
          }
        >
          Reset
        </Button>
      </div>

      <div className="mt-3 space-y-4">
        <SelectBlock
          label="Source"
          value={filters.source ?? ""}
          options={[["", "All sources"], ...COMMUNITY_SOURCE_TYPES.map((source) => [source, communitySourceLabel(source)] as const)]}
          onChange={(value) => onChange({ source: value ? (value as CommunitySourceType) : undefined })}
        />

        <SelectBlock
          label="Sentiment"
          value={filters.sentiment ?? ""}
          options={[["", "All sentiment"], ...COMMUNITY_SENTIMENTS.map((sentiment) => [sentiment, communitySentimentLabel(sentiment)] as const)]}
          onChange={(value) => onChange({ sentiment: value ? (value as CommunityTopicSentiment) : undefined })}
        />

        <SelectBlock
          label="Topic"
          value={filters.topic ?? ""}
          options={[["", "All topics"], ...COMMUNITY_TOPIC_KEYS.map((topic) => [topic, communityTopicLabel(topic)] as const)]}
          onChange={(value) => onChange({ topic: value ? (value as CommunityTopicKey) : undefined })}
        />

        <SelectBlock
          label="Sort"
          value={filters.sort ?? "trending"}
          options={COMMUNITY_SORTS.map((sort) => [sort, sortLabel(sort)] as const)}
          onChange={(value) => onChange({ sort: value as CommunitySort })}
        />
      </div>
    </aside>
  )
}

function SelectBlock({
  label,
  value,
  options,
  onChange
}: {
  label: string
  value: string
  options: ReadonlyArray<readonly [string, string]>
  onChange: (value: string) => void
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">{label}</span>
      <NativeSelect className="w-full" value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map(([optionValue, labelText]) => (
          <NativeSelectOption key={optionValue} value={optionValue}>
            {labelText}
          </NativeSelectOption>
        ))}
      </NativeSelect>
    </label>
  )
}

function sortLabel(sort: CommunitySort) {
  const labels: Record<CommunitySort, string> = {
    trending: "Trending",
    hot: "Hot",
    newest: "Newest",
    controversial: "Controversial",
    adoption: "Adoption"
  }
  return labels[sort]
}
