import { DesktopRunReportDetail } from "@/app/reports/desktop-report-detail"
export default async function RunReportPage(
  props:{params: Promise<{runId:string}>;searchParams: Promise<{sourceRevision?:string|string[]}>}
) {
  const searchParams = await props.searchParams;
  const params = await props.params;
  const revision=typeof searchParams.sourceRevision==="string"?searchParams.sourceRevision:"";return revision?<DesktopRunReportDetail runId={decodeURIComponent(params.runId)} sourceRevision={revision}/>:<p role="alert">缺少 sourceRevision，无法核对精确运行报告。</p>
}
