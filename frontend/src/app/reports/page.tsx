import { ReportsPageClient } from "@/app/reports/reports-page-client"
import { DesktopReportsPage } from "@/app/reports/desktop-reports-page"
import { Suspense } from "react"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { isHostedProfile } from "@/lib/deployment-profile"
export default function ReportsPage(){return <Suspense fallback={<PageSkeleton/>}>{isHostedProfile()?<DesktopReportsPage/>:<ReportsPageClient/>}</Suspense>}
