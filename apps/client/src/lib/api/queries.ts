import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"

import { ApiError, apiFetch } from "@/lib/api/client"
import type {
  ActivityResponse,
  Admin,
  AdminsResponse,
  AgentModel,
  AgentModelSelection,
  ListeningChannels,
  Me,
  MentionOnlyChannels,
  Provider,
  ProviderConnection,
  ProviderModelsResponse,
  ProvidersResponse,
  Setup,
  SlackMembersResponse,
  UsageSummary,
  Workspace,
  WorkspacesResponse,
} from "@/lib/api/types"

// Single source of truth for the session: every guard and component reads
// this one cached query. `null` means signed out — a state, not an error —
// so a 401 resolves instead of throwing.
export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async (): Promise<Me | null> => {
      try {
        return await apiFetch<Me>("/me")
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          return null
        }
        throw error
      }
    },
    retry: false,
    staleTime: 5 * 60 * 1000,
  })
}

export function useWorkspaces() {
  return useQuery({
    queryKey: ["workspaces"],
    queryFn: async (): Promise<Workspace[]> =>
      (await apiFetch<WorkspacesResponse>("/workspaces")).workspaces,
    staleTime: 5 * 60 * 1000,
  })
}

export function useUsage(workspaceId: string | null, month: string) {
  return useQuery({
    queryKey: ["usage", workspaceId, month],
    queryFn: () =>
      apiFetch<UsageSummary>(`/workspaces/${workspaceId}/usage?month=${month}`),
    enabled: workspaceId !== null,
  })
}

// Most steps finish in Slack, outside this page, so the checklist polls until
// it is complete. The poll pauses while the tab is hidden.
export function useSetup(workspaceId: string | null) {
  return useQuery({
    queryKey: ["setup", workspaceId],
    queryFn: () => apiFetch<Setup>(`/workspaces/${workspaceId}/setup`),
    enabled: workspaceId !== null,
    refetchInterval: (query) => (query.state.data?.complete ? false : 15_000),
  })
}

export function useActivity(workspaceId: string | null, limit = 50) {
  return useQuery({
    queryKey: ["activity", workspaceId, limit],
    queryFn: async (): Promise<ActivityResponse["items"]> =>
      (
        await apiFetch<ActivityResponse>(
          `/workspaces/${workspaceId}/activity?limit=${limit}`
        )
      ).items,
    enabled: workspaceId !== null,
    refetchInterval: 30_000,
  })
}

export function useLogout() {
  return useMutation({
    mutationFn: () => apiFetch<void>("/auth/logout", { method: "POST" }),
    // Hard navigation (not a router redirect) wipes all client state.
    // Runs even if the request failed — the session may already be gone.
    onSettled: () => {
      window.location.assign("/login")
    },
  })
}

export function useProviders(workspaceId: string) {
  return useQuery({
    queryKey: ["providers", workspaceId, "connections"],
    queryFn: async () => {
      const data = await apiFetch<ProvidersResponse>(
        `/workspaces/${workspaceId}/providers`
      )
      if (!Array.isArray(data?.connections) || !Array.isArray(data.providers)) {
        throw new Error("Invalid provider connections response")
      }
      return data
    },
  })
}

export function useConnectionModels(workspaceId: string, connectionId: string) {
  const queryClient = useQueryClient()
  return useQuery({
    queryKey: ["provider-models", workspaceId, connectionId],
    queryFn: async () => {
      try {
        return (
          await apiFetch<ProviderModelsResponse>(
            `/workspaces/${workspaceId}/provider-connections/${connectionId}/models`
          )
        ).models
      } catch (error) {
        if (
          error instanceof ApiError &&
          (error.detail === "connection_required" ||
            error.detail === "connection_not_found")
        )
          await Promise.all([
            queryClient.invalidateQueries({
              queryKey: ["providers", workspaceId],
            }),
            queryClient.invalidateQueries({
              queryKey: ["agent-model", workspaceId],
            }),
          ])
        throw error
      }
    },
    staleTime: 5 * 60 * 1000,
  })
}

export function useAgentModel(workspaceId: string) {
  return useQuery({
    queryKey: ["agent-model", workspaceId],
    queryFn: async () => {
      const data = await apiFetch<AgentModel>(
        `/workspaces/${workspaceId}/agent/model`
      )
      if (typeof data?.ready !== "boolean")
        throw new Error("Invalid model configuration response")
      return data
    },
  })
}

export function useMentionOnly(workspaceId: string) {
  return useQuery({
    queryKey: ["mention-only", workspaceId],
    queryFn: () =>
      apiFetch<MentionOnlyChannels>(
        `/workspaces/${workspaceId}/agent/mention-only`
      ),
  })
}

export function useSaveMentionOnly(workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (setting: MentionOnlyChannels) =>
      apiFetch<void>(`/workspaces/${workspaceId}/agent/mention-only`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(setting),
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ["mention-only", workspaceId],
      }),
  })
}

export function useListening(workspaceId: string) {
  return useQuery({
    queryKey: ["listening", workspaceId],
    queryFn: () =>
      apiFetch<ListeningChannels>(`/workspaces/${workspaceId}/agent/listening`),
  })
}

export function useSaveListening(workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (setting: ListeningChannels) =>
      apiFetch<void>(`/workspaces/${workspaceId}/agent/listening`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(setting),
      }),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["listening", workspaceId] }),
  })
}

export function useSaveAgentModel(workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (selection: AgentModelSelection | null) =>
      apiFetch<void>(
        `/workspaces/${workspaceId}/agent/model`,
        selection
          ? {
              method: "PUT",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify(selection),
            }
          : { method: "DELETE" }
      ),
    onSuccess: () =>
      queryClient.invalidateQueries({
        queryKey: ["agent-model", workspaceId],
      }),
    onError: async (error) => {
      if (!(error instanceof ApiError)) return
      if (error.detail === "model_unavailable")
        await queryClient.invalidateQueries({
          queryKey: ["provider-models", workspaceId],
        })
      if (
        error.detail === "connection_required" ||
        error.detail === "connection_not_found"
      )
        await Promise.all([
          queryClient.invalidateQueries({
            queryKey: ["providers", workspaceId],
          }),
          queryClient.invalidateQueries({
            queryKey: ["agent-model", workspaceId],
          }),
        ])
    },
  })
}

type ConnectionAction =
  | { type: "create"; provider: Provider["id"]; name: string; apiKey: string }
  | { type: "update"; id: string; name: string; apiKey?: string }
  | { type: "check" | "delete"; id: string }

export function useConnectionAction(workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    gcTime: 0,
    mutationFn: async (action: ConnectionAction) => {
      const base = `/workspaces/${workspaceId}/provider-connections`
      if (action.type === "create" || action.type === "update") {
        return apiFetch<ProviderConnection>(
          action.type === "create" ? base : `${base}/${action.id}`,
          {
            method: action.type === "create" ? "POST" : "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              ...(action.type === "create"
                ? { provider: action.provider }
                : {}),
              name: action.name,
              api_key: action.apiKey,
            }),
          }
        )
      }
      return apiFetch<void>(
        `${base}/${action.id}${action.type === "check" ? "/check" : ""}`,
        { method: action.type === "check" ? "POST" : "DELETE" }
      )
    },
    onSettled: (_data, _error, action) =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ["providers", workspaceId] }),
        queryClient.invalidateQueries({
          queryKey: ["agent-model", workspaceId],
        }),
        ...(action.type === "create"
          ? []
          : [
              queryClient.invalidateQueries({
                queryKey: ["provider-models", workspaceId, action.id],
              }),
            ]),
      ]),
  })
}

export function useAdmins(workspaceId: string) {
  return useQuery({
    queryKey: ["admins", workspaceId],
    queryFn: async (): Promise<Admin[]> =>
      (await apiFetch<AdminsResponse>(`/workspaces/${workspaceId}/admins`))
        .admins,
  })
}

// Each page is a Slack users.list call, and a refetch of an infinite query
// asks again for every loaded page. So the pages live only while the picker
// is open, are never refetched, and a rate limit waits for Retry-After.
export function useSlackMembers(workspaceId: string) {
  return useInfiniteQuery({
    queryKey: ["slack-members", workspaceId],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      apiFetch<SlackMembersResponse>(
        `/workspaces/${workspaceId}/slack-members${pageParam ? `?cursor=${encodeURIComponent(pageParam)}` : ""}`,
        { signal }
      ),
    getNextPageParam: (page) => page.next_cursor,
    retry: (failures, error) =>
      failures < 2 && error instanceof ApiError && error.status === 429,
    retryDelay: (_, error) =>
      error instanceof ApiError ? (error.retryAfter ?? 60) * 1000 : 1000,
    refetchOnWindowFocus: false,
    staleTime: Infinity,
    gcTime: 0,
  })
}

export function useAddAdmin(workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (slackUserId: string) =>
      apiFetch<void>(`/workspaces/${workspaceId}/admins`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ slack_user_id: slackUserId }),
      }),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: ["admins", workspaceId] }),
  })
}

export function useRemoveAdmin(workspaceId: string) {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (slackUserId: string) =>
      apiFetch<void>(
        `/workspaces/${workspaceId}/admins/${encodeURIComponent(slackUserId)}`,
        { method: "DELETE" }
      ),
    // Removing yourself ends your access: refreshing /me and the workspace
    // list lets the route guard show the Start page.
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: ["me"] }),
        queryClient.invalidateQueries({ queryKey: ["workspaces"] }),
      ]),
    onSettled: () =>
      queryClient.invalidateQueries({ queryKey: ["admins", workspaceId] }),
  })
}
