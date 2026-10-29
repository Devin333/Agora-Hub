import { CheckCircle2 } from "lucide-react"
import { Badge } from "@/components/common/badge"
import type { KeyFact } from "@/types/news"

export function KeyFactsList({ facts }: { facts?: KeyFact[] }) {
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <h2 className="text-[15px] font-semibold text-foreground">Key facts</h2>
      {facts?.length ? (
        <ul className="mt-3 divide-y divide-border overflow-hidden rounded-xl border border-border">
          {facts.map((fact) => (
            <li key={fact.id} className="flex gap-3 px-4 py-3">
              <CheckCircle2 size={16} strokeWidth={1.8} className="mt-0.5 shrink-0 text-success" aria-hidden="true" />
              <div className="min-w-0">
                <p className="text-body2 leading-relaxed text-foreground">{fact.text}</p>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {fact.sourceName ? <Badge tone="neutral">{fact.sourceName}</Badge> : null}
                  {fact.confidence ? (
                    <Badge tone={fact.confidence === "high" ? "success" : fact.confidence === "medium" ? "warning" : "danger"}>
                      {fact.confidence} confidence
                    </Badge>
                  ) : null}
                </div>
              </div>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-body2 text-muted-foreground">No key facts have been extracted yet.</p>
      )}
    </section>
  )
}
