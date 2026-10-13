import { Suspense } from "react"
import { PortalHomePage } from "@/features/portal/portal-home-page"
import { getPortalHomeData } from "@/features/portal/portal-home-data"
import { DesignDemoPage } from "@/features/portal/components/design-demo-page"
import { isLocalDesktopProfile } from "@/lib/deployment-profile"

export const dynamic = "force-dynamic"

export default async function HomePage() {
  if (isLocalDesktopProfile()) return <Suspense><DesignDemoPage /></Suspense>
  return <PortalHomePage data={await getPortalHomeData()} />
}
