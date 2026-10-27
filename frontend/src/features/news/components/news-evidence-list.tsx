import { ExternalLink } from "lucide-react"
import { CredibilityBadge } from "@/components/common/credibility-badge"
import { SourceBadge } from "@/components/common/source-badge"
import { Button } from "@/components/ui/button"
import { formatDateTime } from "@/lib/format"
import type { EvidenceItem } from "@/types/evidence"

export function NewsEvidenceList({ evidence }: { evidence: EvidenceItem[] }) {
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <h2 className="text-[15px] font-semibold text-foreground">Evidence</h2>
      {evidence.length ? (
        <div className="mt-3 divide-y divide-border overflow-hidden rounded-xl border border-border">
          {evidence.map((item) => (
            <article key={item.id} className="px-4 py-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <h3 className="text-[13.5px] font-medium text-foreground">{item.title}</h3>
                  <p className="mt-1 text-[13px] leading-relaxed text-muted-foreground">{item.summary}</p>
                </div>
                {item.originalUrl || item.sourceUrl ? (
                  <Button asChild variant="ghost" size="icon-sm" className="shrink-0 text-muted-foreground">
                    <a href={item.originalUrl ?? item.sourceUrl} target="_blank" rel="noreferrer" aria-label="Open original evidence source">
                      <ExternalLink size={15} strokeWidth={1.8} />
                    </a>
                  </Button>
                ) : null}
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-1.5">
                <SourceBadge name={item.sourceName} type={item.sourceType} />
                <CredibilityBadge value={item.credibility} />
                <span className="text-label text-muted-foreground">Captured {formatDateTime(item.capturedAt)}</span>
              </div>
              <p className="mt-2 text-label text-muted-foreground">Relation: {item.relationReason}</p>
            </article>
          ))}
        </div>
      ) : (
        <p className="mt-2 text-body2 text-muted-foreground">No evidence is linked to this news item yet.</p>
      )}
    </section>
  )
}
