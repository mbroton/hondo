import { useOutletContext } from "react-router"
import type { MCPConnection, Workspace } from "@/lib/api/types"

export type MCPPageContext = {
  workspace: Workspace
  connections: MCPConnection[]
}

export function useMCPPage() {
  return useOutletContext<MCPPageContext>()
}

export function connectorsPath(workspaceId: string, connectionId?: string) {
  return `/workspaces/${workspaceId}/connectors${connectionId ? `/${connectionId}` : ""}`
}

export function setupPath(workspaceId: string, connectionId: string) {
  return `${connectorsPath(workspaceId, connectionId)}/setup`
}

export function toolPath(
  workspaceId: string,
  connectionId: string,
  tool?: string
) {
  return `${connectorsPath(workspaceId, connectionId)}/tools/${tool ? encodeURIComponent(tool) : "new"}`
}
