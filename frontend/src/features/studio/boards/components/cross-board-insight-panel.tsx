import { GitMerge, RadioTower, Route, UsersRound } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import type { StudioCrossBoardViewModel } from "@/types/board"

export function CrossBoardInsightPanel({ crossBoard }: { crossBoard: StudioCrossBoardViewModel }) {
  return (
    <section className="space-y-4">
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <SignalColumn icon={GitMerge} title="Cross-board associations" items={crossBoard.associations} />
        <SignalColumn icon={Route} title="Trend paths" items={crossBoard.trendPaths} />
        <SignalColumn icon={UsersRound} title="Shared entities" items={crossBoard.sharedEntities} />
        <SignalColumn icon={RadioTower} title="Conflict signals" items={crossBoard.conflictSignals} />
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Integrated report entry</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm font-medium text-foreground">{crossBoard.reportTitle ?? "Cross Board report"}</p>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {crossBoard.reportSummary ?? "Report summary will populate from BoardOutput metadata when available."}
          </p>
          {crossBoard.notices.length ? (
            <div className="mt-4 space-y-2 rounded-md border border-warning/30 bg-warning/10 p-3">
              {crossBoard.notices.map((notice) => (
                <p key={notice} className="text-sm text-warning">
                  {notice}
                </p>
              ))}
            </div>
          ) : null}
        </CardContent>
      </Card>
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
    <Card>
      <CardHeader className="flex-row items-center gap-3">
        <Icon className="size-5 text-link" />
        <CardTitle className="text-sm">{title}</CardTitle>
      </CardHeader>
      <CardContent>
        <ul className="space-y-2 text-sm text-muted-foreground">
          {items.map((item) => (
            <li key={item} className="rounded-md border border-border bg-background px-3 py-2">
              {item}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  )
}
