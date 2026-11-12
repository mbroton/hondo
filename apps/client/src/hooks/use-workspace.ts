import { useWorkspaces } from "@/lib/api/queries"
import type { Workspace } from "@/lib/api/types"

export type WorkspaceState = {
  workspace: Workspace | null
  isPending: boolean
  isError: boolean
}

/** The one Slack workspace this deployment serves, once it is installed. */
export function useWorkspace(): WorkspaceState {
  const { data: workspaces, isPending, isError } = useWorkspaces()
  return {
    workspace: workspaces?.[0] ?? null,
    isPending,
    isError: isError && workspaces === undefined,
  }
}
