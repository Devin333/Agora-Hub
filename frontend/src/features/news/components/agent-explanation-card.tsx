import { Bot } from "lucide-react"

export function AgentExplanationCard({ items }: { items?: string[] }) {
  return (
    <section className="rounded-md border border-[#dbe3dc] bg-white/85 p-5 dark:border-border dark:bg-card">
      <div className="flex items-center gap-2">
        <Bot className="h-4 w-4 text-emerald-700 dark:text-link" />
        <h2 className="text-lg font-semibold text-[#334155] dark:text-foreground">Agent explanation</h2>
      </div>
      {items?.length ? (
        <ul className="mt-4 list-disc space-y-2 pl-5 text-sm leading-6 text-[#334155]/68 dark:text-muted-foreground">
          {items.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-3 text-sm text-[#334155]/60 dark:text-muted-foreground">No public agent explanation is available yet.</p>
      )}
    </section>
  )
}
