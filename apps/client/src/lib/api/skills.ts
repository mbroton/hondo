import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ApiError, apiFetch } from "@/lib/api/client"
import type { Skill, SkillUpdate, Skills } from "@/lib/api/types"

export function useSkills(workspaceId: string) {
  return useQuery({
    queryKey: ["skills", workspaceId],
    queryFn: ({ signal }) =>
      apiFetch<Skills>(`/workspaces/${workspaceId}/skills`, { signal }),
  })
}

export function useSkill(workspaceId: string, skillId: string) {
  return useQuery({
    queryKey: ["skills", workspaceId, skillId],
    queryFn: ({ signal }) =>
      apiFetch<Skill>(`/workspaces/${workspaceId}/skills/${skillId}`, {
        signal,
      }),
    retry: (failures, error) =>
      failures < 3 &&
      !(error instanceof ApiError && [401, 404].includes(error.status)),
  })
}

type Action =
  | { type: "create"; body: { content: string } }
  | { type: "save"; id: string; body: SkillUpdate }
  | { type: "delete"; id: string }

export function useSkillAction(workspaceId: string) {
  const cache = useQueryClient()
  const listKey = ["skills", workspaceId]
  return useMutation({
    mutationFn: (action: Action) => {
      const base = `/workspaces/${workspaceId}/skills`
      return apiFetch<Skill | undefined>(
        action.type === "create" ? base : `${base}/${action.id}`,
        {
          method:
            action.type === "create"
              ? "POST"
              : action.type === "save"
                ? "PUT"
                : "DELETE",
          headers: { "Content-Type": "application/json" },
          body: "body" in action ? JSON.stringify(action.body) : undefined,
        }
      )
    },
    onSuccess: async (result, action) => {
      if (action.type === "delete") {
        const skillKey = ["skills", workspaceId, action.id]
        await cache.cancelQueries({ queryKey: skillKey, exact: true })
        cache.removeQueries({ queryKey: skillKey, exact: true })
        await cache.cancelQueries({ queryKey: listKey, exact: true })
        cache.setQueryData<Skills>(listKey, (current) =>
          current
            ? {
                skills: current.skills.filter((item) => item.id !== action.id),
              }
            : current
        )
      } else if (result) {
        const skillKey = ["skills", workspaceId, result.id]
        await cache.cancelQueries({ queryKey: skillKey, exact: true })
        cache.setQueryData<Skill>(skillKey, result)
      }
    },
    onSettled: (_result, error, action) => {
      if (error && action.type === "save")
        void cache.invalidateQueries({
          queryKey: ["skills", workspaceId, action.id],
        })
      return cache.invalidateQueries({ queryKey: listKey, exact: true })
    },
  })
}
