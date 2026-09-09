import { DesignDemoPage } from "@/features/portal/components/design-demo-page"
import { Suspense } from "react"

export const dynamic = "force-static"

export default function DesignDemoRoute() {
  return <Suspense><DesignDemoPage /></Suspense>
}
