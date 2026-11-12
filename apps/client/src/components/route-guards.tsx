import type { ReactNode } from "react"
import { Navigate, Outlet, useLocation, useSearchParams } from "react-router"

import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { useMe } from "@/lib/api/queries"

function FullPageSkeleton() {
  return (
    <div className="flex min-h-svh flex-col gap-4 p-4 md:p-6">
      <div className="flex items-center gap-2">
        <Skeleton className="size-8 rounded-lg" />
        <Skeleton className="h-4 w-32" />
      </div>
      <Skeleton className="h-32 w-full" />
      <Skeleton className="h-32 w-full" />
    </div>
  )
}

function FullPageError({ onRetry }: { onRetry: () => void }) {
  return (
    <main className="flex min-h-svh items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <CardHeader className="text-center">
          <CardTitle>Can't reach the server</CardTitle>
          <CardDescription>
            Something went wrong while loading your session. Check your
            connection and try again.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex justify-center">
          <Button variant="outline" onClick={onRetry}>
            Retry
          </Button>
        </CardContent>
      </Card>
    </main>
  )
}

export function RequireAuth() {
  const { data: me, isPending, isError, refetch } = useMe()
  const { search } = useLocation()

  if (isPending) {
    return <FullPageSkeleton />
  }

  // Only an explicit null means signed out; a failed /me query (network or
  // server trouble) must not bounce a possibly logged-in user to /login.
  if (isError) {
    return <FullPageError onRetry={() => void refetch()} />
  }

  // The query carries install outcomes (`slack_error`), which the login page
  // shows to someone who installed before they signed in.
  if (!me) {
    return <Navigate to={`/login${search}`} replace />
  }

  return <Outlet />
}

export function RedirectIfAuthed() {
  const { data: me, isPending } = useMe()
  const [params] = useSearchParams()

  if (isPending) {
    return <FullPageSkeleton />
  }

  if (me && !params.has("error")) {
    return <Navigate to="/" replace />
  }

  return <Outlet />
}

// Renders under RequireAuth, so /me has resolved and `me` is set. Only the
// admins of an installed workspace get the dashboard; everyone else gets the
// fallback in place, so the URL (and its install notices) stays as it is.
export function RequireAdmin({ fallback }: { fallback: ReactNode }) {
  const { data: me } = useMe()

  if (!me?.team.is_admin || !me.team.workspace_id) {
    return fallback
  }

  return <Outlet />
}
