import { Navigate, Outlet, useParams } from "react-router"
import { ErrorCard } from "@/components/error-card"
import { Skeleton } from "@/components/ui/skeleton"
import { useWorkspace } from "@/hooks/use-workspace"
import { skillsPath, type SkillsPageContext } from "./context"

export default function SkillsLayout() {
  const { workspaceId } = useParams()
  const selection = useWorkspace()
  const { workspace } = selection

  if (selection.isPending) return <Skeleton className="h-64 w-full" />
  if (selection.isError || !workspace)
    return <ErrorCard title="Could not load your workspace" />
  if (!workspaceId)
    return <Navigate to={skillsPath(workspace.id)} replace />
  if (workspaceId !== workspace.id)
    return <ErrorCard title="Workspace not found" />
  const context: SkillsPageContext = { workspace }
  return (
    <div key={workspace.id} className="flex w-full max-w-6xl flex-col gap-6">
      <Outlet context={context} />
    </div>
  )
}
