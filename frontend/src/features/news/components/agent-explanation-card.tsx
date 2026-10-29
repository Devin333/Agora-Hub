import { Bot } from "lucide-react"

export function AgentExplanationCard({ items }: { items?: string[] }) {
  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <div className="flex items-center gap-2">
        <Bot size={16} strokeWidth={1.8} className="text-muted-foreground" aria-hidden="true" />
        <h2 className="text-[15px] font-semibold text-foreground">Agent explanation</h2>
      </div>
      {items?.length ? (
        <ul className="mt-3 list-disc space-y-1.5 pl-5 text-body2 leading-relaxed text-muted-foreground marker:text-faint-foreground">
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-body2 text-muted-foreground">No public agent explanation is available yet.</p>
      )}
    </section>
  )
}
