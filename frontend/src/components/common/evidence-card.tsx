import { CredibilityBadge, ExternalTextLink, SourceBadge } from "@/components/common/badges";
import { ScoreMeter } from "@/components/common/score-meter";
import { formatDateTime } from "@/lib/format";
import type { Evidence } from "@/types/evidence";

export function EvidenceCard({ evidence }: { evidence: Evidence }) {
  return (
    <article className="flex flex-col gap-3 rounded-2xl border border-border bg-card p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-[15px] font-semibold text-foreground">{evidence.title}</h3>
          <p className="mt-1 text-label text-muted-foreground">捕获于 {formatDateTime(evidence.capturedAt)}</p>
        </div>
        <div className="flex flex-wrap gap-1.5">
          <SourceBadge sourceName={evidence.sourceName} sourceType={evidence.sourceType} />
          <CredibilityBadge credibility={evidence.credibility} />
        </div>
      </div>
      <p className="text-body2 leading-relaxed text-muted-foreground">{evidence.quote ?? evidence.summary}</p>
      <p className="rounded-xl bg-muted px-3 py-2 text-label text-muted-foreground">{evidence.relationReason}</p>
      <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
        <ScoreMeter label="置信度" value={evidence.confidenceScore ?? 0} />
        <ExternalTextLink href={evidence.originalUrl ?? evidence.sourceUrl ?? "#"}>原始来源</ExternalTextLink>
      </div>
    </article>
  );
}
