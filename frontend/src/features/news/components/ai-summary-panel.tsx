export function AISummaryPanel({
  summary,
  whyItMatters,
}: {
  summary?: string
  whyItMatters?: string
}) {
  return (
    <section className="rounded-md border border-[#dbe3dc] bg-white/85 p-5 dark:border-border dark:bg-card">
      <h2 className="text-lg font-semibold text-[#334155] dark:text-foreground">AI summary</h2>
      <p className="mt-3 text-sm leading-6 text-[#334155]/68 dark:text-muted-foreground">
        {summary ?? "This news item does not have a detailed AI summary yet."}
      </p>
      <div className="mt-5 rounded-md border border-[#edf1ed] bg-[#f7f9f6] p-4 dark:border-border dark:bg-background">
        <p className="text-xs font-medium uppercase text-emerald-700 dark:text-link">Why it matters</p>
        <p className="mt-2 text-sm leading-6 text-[#334155] dark:text-foreground">{whyItMatters ?? "No importance analysis is available yet."}</p>
      </div>
    </section>
  )
}
