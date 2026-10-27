import { Skeleton } from "@/components/ui/skeleton"
import { cn } from "@/lib/utils"

export function LoadingSkeleton({ rows = 4, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn("space-y-4", className)} aria-busy="true">
      <Skeleton className="h-7 w-48" />
      <div className="divide-y divide-border overflow-hidden rounded-xl border border-border">
        {Array.from({ length: rows }).map((_, index) => (
          <div key={index} className="px-4 py-3">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="mt-2.5 h-3 w-full" />
            <Skeleton className="mt-2 h-3 w-5/6" />
          </div>
        ))}
      </div>
    </div>
  )
}

export function PageSkeleton() {
  return <LoadingSkeleton rows={5} />
}
