import { API_BASE } from "@/lib/api/client"

// Both URLs are full-page navigations: the OAuth round trips set cookies
// server-side, so a client-side route change can't stand in for them.
export function loginUrl(options: { team?: string; next?: string } = {}) {
  const params = new URLSearchParams()
  if (options.team) params.set("team", options.team)
  if (options.next) params.set("next", options.next)
  const query = params.toString()
  return `${API_BASE}/auth/slack/login${query ? `?${query}` : ""}`
}

export function slackInstallUrl(): string {
  return `${API_BASE}/slack/install`
}
