import { GitMerge, RadioTower, Route, UsersRound } from "lucide-react"
import { StudioNotice, StudioPanel } from "@/features/studio/shared/components/studio-dashboard"
import type { StudioCrossBoardViewModel } from "@/types/board"

export function CrossBoardInsightPanel({ crossBoard }: { crossBoard: StudioCrossBoardViewModel }) {
  return (
    <section className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <SignalColumn icon={GitMerge} title="Cross-board associations" items={crossBoard.associations} />
        <SignalColumn icon={Route} title="Trend paths" items={crossBoard.trendPaths} />
        <SignalColumn icon={UsersRound} title="Shared entities" items={crossBoard.sharedEntities} />
        <SignalColumn icon={RadioTower} title="Conflict signals" items={crossBoard.conflictSignals} />
      </div>

      <StudioPanel title="Integrated report entry" contentClassName="space-y-3">
        <div>
          <p className="text-[13.5px] font-medium text-foreground">{crossBoard.reportTitle ?? "Cross Board report"}</p>
          <p className="mt-1 text-body2 text-muted-foreground">
            {crossBoard.reportSummary ?? "Report summary will populate from BoardOutput metadata when available."}
          </p>
        </div>
        {crossBoard.notices.length ? (
          <StudioNotice>
            <div className="space-y-1">
              {crossBoard.notices.map((notice) => (
                <p key={notice}>{notice}</p>
              ))}
            </div>
          </StudioNotice>
        ) : null}
      </StudioPanel>
    </section>
  )
}

function SignalColumn({
  icon: Icon,
  title,
  items
}: {
  icon: typeof GitMerge
  title: string
  items: string[]
}) {
  return (
    <section className="min-w-0 rounded-2xl border border-border bg-card p-4">
      <div className="mb-3 flex items-center gap-2">
        <Icon size={16} strokeWidth={1.8} aria-hidden="true" className="shrink-0 text-muted-foreground" />
        <h3 className="truncate text-[13.5px] font-semibold text-foreground">{title}</h3>
      </div>
      {items.length ? (
        <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border text-body2 text-muted-foreground">
          {items.map((item) => (
            <li key={item} className="px-3 py-2">
              {item}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  )
}
