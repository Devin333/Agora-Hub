import type { ReactNode } from "react"
import { StudioSidebar } from "@/features/studio/shared/components/studio-sidebar"
import { StudioTopbar } from "@/features/studio/shared/components/studio-topbar"

export function StudioShell({ children }: { children: ReactNode }) {
  return (
    <div className="flex min-h-screen bg-background text-foreground">
      <StudioSidebar />
      <div className="min-w-0 flex-1">
        <StudioTopbar />
        <main className="min-w-0 px-4 py-5 md:px-6">
          <div className="mx-auto w-full max-w-[1600px]">{children}</div>
        </main>
      </div>
    </div>
  )
}
