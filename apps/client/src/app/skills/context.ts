import { useOutletContext } from "react-router"
import type { Workspace } from "@/lib/api/types"

export type SkillsPageContext = {
  workspace: Workspace
}

export function useSkillsPage() {
  return useOutletContext<SkillsPageContext>()
}

export function skillsPath(workspaceId: string, skillId?: string) {
  return `/workspaces/${workspaceId}/skills${skillId ? `/${skillId}` : ""}`
}
