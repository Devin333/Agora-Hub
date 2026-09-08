import { ReportsPageClient } from "@/app/reports/reports-page-client";
import { Suspense } from "react";
import { PageSkeleton } from "@/components/common/loading-skeleton";

export default function ReportsPage() {
  return <Suspense fallback={<PageSkeleton />}><ReportsPageClient /></Suspense>;
}
