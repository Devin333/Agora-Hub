import { BoardDetailPage } from "@/features/studio/boards/components/board-center-page"
import { listBoardDefinitions } from "@/features/studio/boards/api/board-center-server-api"
import { buildBoardDetailViewModel } from "@/features/studio/boards/lib/board-adapter"

export default async function StudioBoardDetailRoute(props: { params: Promise<{ boardType: string }> }) {
  const params = await props.params;
  const boardList = await listBoardDefinitions()
  const detail = buildBoardDetailViewModel(params.boardType, boardList)
  return <BoardDetailPage detail={detail} />
}
