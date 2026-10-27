import { StudioNotice } from "@/features/studio/shared/components/studio-dashboard"

export function ArtifactNotice({ notices }: { notices: string[] }) {
  if (!notices.length) return null

  return (
    <StudioNotice>
      <div className="space-y-1">
        {notices.map((notice) => (
          <p key={notice}>{notice}</p>
        ))}
      </div>
    </StudioNotice>
  )
}
