import { ReportsPageClient } from "@/app/reports/reports-page-client"
import { DesktopReportsPage } from "@/app/reports/desktop-reports-page"
import { Suspense } from "react"
import { PageSkeleton } from "@/components/common/loading-skeleton"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"
export default function ReportsPage(){return <Suspense fallback={<PageSkeleton/>}>{isLocalDesktopProfile()?<DesktopReportsPage/>:<ReportsPageClient/>}</Suspense>}
