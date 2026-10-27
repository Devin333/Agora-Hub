export function AISummaryPanel({
  summary,
  whyItMatters,
}: {
  summary?: string
  whyItMatters?: string
}) {
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <h2 className="text-[15px] font-semibold text-foreground">AI summary</h2>
      <p className="mt-2 text-body1 leading-relaxed text-foreground/85">
        {summary ?? "This news item does not have a detailed AI summary yet."}
      </p>
      <div className="mt-4 rounded-xl bg-muted px-4 py-3">
        <p className="text-[11px] font-semibold uppercase tracking-wider text-faint-foreground">Why it matters</p>
        <p className="mt-1 text-body2 leading-relaxed text-foreground">{whyItMatters ?? "No importance analysis is available yet."}</p>
      </div>
    </section>
  )
}
