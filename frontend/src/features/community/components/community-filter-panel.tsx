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
    <aside className="rounded-md border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-foreground">Filters</h2>
        <button
          type="button"
          onClick={() =>
            onChange({
              source: undefined,
              sentiment: undefined,
              topic: undefined,
              sort: "trending"
            })
          }
          className="text-xs text-link hover:text-foreground"
        >
          Reset
        </button>
      </div>

      <div className="mt-4 space-y-5">
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
      <span className="mb-2 block text-xs font-medium uppercase text-muted-foreground">{label}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-10 w-full rounded-md border border-border bg-background px-3 text-sm text-foreground outline-none"
      >
        {options.map(([optionValue, labelText]) => (
          <option key={optionValue} value={optionValue}>
            {labelText}
          </option>
        ))}
      </select>
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
