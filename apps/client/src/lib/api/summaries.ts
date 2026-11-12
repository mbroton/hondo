import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query"
import { apiFetch } from "@/lib/api/client"
import type { ThreadSummaries } from "@/lib/api/types"

export function useThreadSummaries(workspaceId: string, search: string) {
  return useInfiniteQuery({
    queryKey: ["thread-summaries", workspaceId, search],
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) => {
      const params = new URLSearchParams()
      if (search) params.set("q", search)
      if (pageParam) params.set("offset", String(pageParam))
      const query = params.size ? `?${params}` : ""
      return apiFetch<ThreadSummaries>(
        `/workspaces/${workspaceId}/thread-summaries${query}`,
        { signal }
      )
    },
    getNextPageParam: (page) => page.next_offset,
  })
}

export function useDeleteThreadSummary(workspaceId: string) {
  const cache = useQueryClient()
  const queryKey = ["thread-summaries", workspaceId]
  return useMutation({
    mutationFn: (id: number) =>
      apiFetch<void>(`/workspaces/${workspaceId}/thread-summaries/${id}`, {
        method: "DELETE",
      }),
    onSuccess: async (_result, id) => {
      await cache.cancelQueries({ queryKey })
      cache.setQueriesData<InfiniteData<ThreadSummaries>>(
        { queryKey },
        (current) =>
          current && {
            ...current,
            pages: current.pages.map((page) => ({
              ...page,
              summaries: page.summaries.filter((item) => item.id !== id),
            })),
          }
      )
    },
    onSettled: () => cache.invalidateQueries({ queryKey }),
  })
}
