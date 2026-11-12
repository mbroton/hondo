import {
  Navigate,
  Outlet,
  useLocation,
  useParams,
  useSearchParams,
} from "react-router"
import { ErrorCard } from "@/components/error-card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useWorkspace } from "@/hooks/use-workspace"
import { useMCPConnections } from "@/lib/api/mcp"
import type { Workspace } from "@/lib/api/types"
import { connectorsPath, type MCPPageContext } from "./context"

function OAuthNotice() {
  const [params, setParams] = useSearchParams()
  const outcome = params.get("mcp")
  if (!outcome) return null
  return (
    <Alert>
      <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
        <span>
          {outcome === "connected"
            ? "Account connected. Review the tools and channel access."
            : outcome === "cancelled"
              ? "Sign-in was cancelled. Your connector is still saved."
              : "Sign-in could not be completed. Try connecting the account again."}
        </span>
        <Button
          variant="ghost"
          size="sm"
          onClick={() =>
            setParams(
              (previous) => {
                const next = new URLSearchParams(previous)
                if (outcome === "connected" && !next.has("tab"))
                  next.set("tab", "tools")
                next.delete("mcp")
                return next
              },
              { replace: true }
            )
          }
        >
          Dismiss
        </Button>
      </AlertDescription>
    </Alert>
  )
}

function WorkspaceMCPPage({ workspace }: { workspace: Workspace }) {
  const query = useMCPConnections(workspace.id)
  if (query.isPending) return <Skeleton className="h-64 w-full" />
  if (!query.data) {
    return (
      <ErrorCard
        title="Could not load connectors"
        onRetry={() => void query.refetch()}
        retrying={query.isFetching}
      />
    )
  }
  const context: MCPPageContext = {
    workspace,
    connections: query.data.connections,
  }
  return (
    <div className="flex w-full max-w-6xl flex-col gap-6">
      <OAuthNotice />
      {query.isError && (
        <Alert variant="destructive">
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>
              Could not refresh connectors. Your unsaved changes are kept.
            </span>
            <Button
              variant="outline"
              size="sm"
              disabled={query.isFetching}
              onClick={() => void query.refetch()}
            >
              {query.isFetching ? "Retrying..." : "Retry"}
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <Outlet context={context} />
    </div>
  )
}

export default function MCPLayout() {
  const { workspaceId } = useParams()
  const selection = useWorkspace()
  const { search } = useLocation()
  const { workspace } = selection

  if (selection.isPending) return <Skeleton className="h-64 w-full" />
  if (selection.isError || !workspace)
    return <ErrorCard title="Could not load your workspace" />
  if (!workspaceId) {
    return (
      <Navigate
        to={`${connectorsPath(workspace.id)}${search}`}
        replace
      />
    )
  }
  if (workspaceId !== workspace.id)
    return <ErrorCard title="Workspace not found" />
  return <WorkspaceMCPPage key={workspace.id} workspace={workspace} />
}

export function MCPRedirect() {
  const { workspaceId, connectionId } = useParams()
  const { search } = useLocation()
  const target = workspaceId
    ? connectorsPath(workspaceId, connectionId)
    : "/connectors"
  return <Navigate to={`${target}${search}`} replace />
}
