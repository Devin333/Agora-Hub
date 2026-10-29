import { Badge, type BadgeVariant } from "@/components/ui/badge"
import { communitySentimentLabel } from "@/lib/community/community-filters"
import type { CommunityTopicSentiment } from "@/types/community"

const sentimentVariant: Record<CommunityTopicSentiment, BadgeVariant> = {
  positive: "success",
  negative: "danger",
  mixed: "warning",
  controversial: "warning",
  neutral: "muted",
  unknown: "outline"
}

export function CommunitySentimentBadge({ sentiment }: { sentiment: CommunityTopicSentiment }) {
  return <Badge variant={sentimentVariant[sentiment]}>{communitySentimentLabel(sentiment)}</Badge>
}
