import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"
import { ApiError, apiFetch } from "@/lib/api/client"
import type {
  BuiltinConnectors,
  HTTPConnectionCreate,
  HTTPTestResult,
  HTTPTool,
  MCPConnection,
  MCPChannelsResponse,
  MCPConnections,
  MCPKeyHeader,
  MCPPolicy,
} from "@/lib/api/types"

export function useMCPConnections(workspaceId: string) {
  return useQuery({
    queryKey: ["mcp", workspaceId],
    queryFn: ({ signal }) =>
      apiFetch<MCPConnections>(`/workspaces/${workspaceId}/mcp-connections`, {
        signal,
      }),
  })
}

export function useBuiltinConnectors(workspaceId: string, enabled = true) {
  return useQuery({
    queryKey: ["builtin-connectors", workspaceId],
    queryFn: ({ signal }) =>
      apiFetch<BuiltinConnectors>(
        `/workspaces/${workspaceId}/builtin-connectors`,
        { signal }
      ),
    enabled,
  })
}

export function useMCPChannels(workspaceId: string) {
  return useInfiniteQuery({
    queryKey: ["mcp-channels", workspaceId],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      apiFetch<MCPChannelsResponse>(
        `/workspaces/${workspaceId}/mcp-channels${pageParam ? `?cursor=${encodeURIComponent(pageParam)}` : ""}`,
        { signal }
      ),
    getNextPageParam: (page) => page.next_cursor,
    retry: (failures, error) =>
      failures < 2 && error instanceof ApiError && error.status === 429,
    retryDelay: (_, error) =>
      error instanceof ApiError ? (error.retryAfter ?? 60) * 1000 : 1000,
    refetchOnWindowFocus: false,
    staleTime: 60_000,
  })
}

export function needsSlackReinstall(error: unknown) {
  return (
    error instanceof ApiError && error.detail === "workspace_reconnect_required"
  )
}

type Action =
  | { type: "create"; body: { name: string; url: string } }
  | {
      type: "builtin"
      body: { builtin: string; name: string; inputs: Record<string, string> }
    }
  | { type: "save"; id: string; body: MCPPolicy }
  | { type: "key"; id: string; body: { headers: MCPKeyHeader[] } }
  | {
      type: "builtin-key"
      id: string
      body: { inputs: Record<string, string> }
    }
  | { type: "refresh" | "disconnect" | "delete"; id: string }
  | { type: "http"; body: HTTPConnectionCreate }
  | { type: "add-tool"; id: string; body: HTTPTool }
  | { type: "save-tool"; id: string; tool: string; body: HTTPTool }
  | { type: "delete-tool"; id: string; tool: string }

function request(action: Action): { path: string; method: string } {
  switch (action.type) {
    case "create":
      return { path: "", method: "POST" }
    case "builtin":
      return { path: "/builtin", method: "POST" }
    case "save":
      return { path: `/${action.id}`, method: "PUT" }
    case "key":
    case "builtin-key":
      return { path: `/${action.id}/${action.type}`, method: "PUT" }
    case "refresh":
    case "disconnect":
      return { path: `/${action.id}/${action.type}`, method: "POST" }
    case "delete":
      return { path: `/${action.id}`, method: "DELETE" }
    case "http":
      return { path: "/http", method: "POST" }
    case "add-tool":
      return { path: `/${action.id}/http-tools`, method: "POST" }
    case "save-tool":
      return {
        path: `/${action.id}/http-tools/${encodeURIComponent(action.tool)}`,
        method: "PUT",
      }
    case "delete-tool":
      return {
        path: `/${action.id}/http-tools/${encodeURIComponent(action.tool)}`,
        method: "DELETE",
      }
  }
}

export function useHTTPToolTest(workspaceId: string, connectionId: string) {
  return useMutation({
    mutationFn: ({
      tool,
      argumentValues,
    }: {
      tool: string
      argumentValues: Record<string, string>
    }) =>
      apiFetch<HTTPTestResult>(
        `/workspaces/${workspaceId}/mcp-connections/${connectionId}/http-tools/${encodeURIComponent(tool)}/test`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ arguments: argumentValues }),
        }
      ),
  })
}

export function useMCPAction(workspaceId: string) {
  const cache = useQueryClient()
  return useMutation({
    mutationFn: (action: Action) => {
      const { path, method } = request(action)
      return apiFetch<MCPConnection | undefined>(
        `/workspaces/${workspaceId}/mcp-connections${path}`,
        {
          method,
          headers: { "Content-Type": "application/json" },
          body: "body" in action ? JSON.stringify(action.body) : undefined,
        }
      )
    },
    onSuccess: async (result, action) => {
      if (!result && action.type !== "delete") return
      const queryKey = ["mcp", workspaceId]
      await cache.cancelQueries({ queryKey })
      cache.setQueryData<MCPConnections>(queryKey, (current) => {
        if (!current) return current
        if (action.type === "delete") {
          return {
            ...current,
            connections: current.connections.filter(
              (item) => item.id !== action.id
            ),
          }
        }
        if (!result) return current
        const exists = current.connections.some((item) => item.id === result.id)
        return {
          ...current,
          connections: exists
            ? current.connections.map((item) =>
                item.id === result.id && item.revision <= result.revision
                  ? result
                  : item
              )
            : [...current.connections, result],
        }
      })
    },
    onSettled: () =>
      cache.invalidateQueries({ queryKey: ["mcp", workspaceId] }),
  })
}
