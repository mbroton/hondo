import { useRef, useState, type FormEvent, type RefObject } from "react"
import { Navigate, useParams } from "react-router"
import { toast } from "sonner"
import {
  RiBrainLine,
  RiDeleteBinLine,
  RiExternalLinkLine,
  RiLockLine,
  RiSearchLine,
} from "@remixicon/react"
import { ErrorCard } from "@/components/error-card"
import { PageHeading } from "@/components/page-heading"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupInput,
} from "@/components/ui/input-group"
import { Skeleton } from "@/components/ui/skeleton"
import { useWorkspace } from "@/hooks/use-workspace"
import { ApiError } from "@/lib/api/client"
import { useDeleteThreadSummary, useThreadSummaries } from "@/lib/api/summaries"
import type { ThreadSummary, Workspace } from "@/lib/api/types"
import { formatRelativeTime } from "@/lib/format"

function memoryPath(workspaceId: string) {
  return `/workspaces/${workspaceId}/memory`
}

function SummaryItem({
  workspaceId,
  summary,
  now,
  returnFocus,
}: {
  workspaceId: string
  summary: ThreadSummary
  now: number
  returnFocus: RefObject<HTMLInputElement | null>
}) {
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const remove = useDeleteThreadSummary(workspaceId)
  const label = summary.private ? "Private channel" : `#${summary.channel}`

  async function confirm() {
    setError(null)
    try {
      await remove.mutateAsync(summary.id)
    } catch (cause) {
      if (!(
        cause instanceof ApiError && cause.detail === "summary_not_found"
      )) {
        setError("The summary could not be deleted. Try again.")
        return
      }
    }
    setConfirming(false)
    toast.success("Summary deleted.")
    requestAnimationFrame(() => returnFocus.current?.focus())
  }

  return (
    <li className="flex flex-col gap-2 px-4 py-4">
      <div className="flex min-w-0 items-center gap-3">
        {summary.url ? (
          <a
            href={summary.url}
            target="_blank"
            rel="noreferrer"
            className="flex min-w-0 items-center gap-1.5 rounded-sm text-sm font-medium underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
          >
            <span className="truncate">{label}</span>
            <RiExternalLinkLine
              className="size-4 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
            <span className="sr-only">(opens the thread in Slack)</span>
          </a>
        ) : (
          <span className="flex min-w-0 items-center gap-1.5 text-sm font-medium text-muted-foreground">
            <RiLockLine className="size-4 shrink-0" aria-hidden="true" />
            {label}
          </span>
        )}
        <time
          className="ml-auto shrink-0 text-xs text-muted-foreground"
          dateTime={summary.updated_at}
          title={new Date(summary.updated_at).toLocaleString()}
        >
          {formatRelativeTime(summary.updated_at, now)}
        </time>
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={`Delete summary from ${label}`}
          disabled={remove.isPending}
          onClick={() => {
            setError(null)
            setConfirming(true)
          }}
        >
          <RiDeleteBinLine />
        </Button>
      </div>
      {summary.text !== null && (
        <p className="max-w-prose text-sm leading-6 wrap-anywhere whitespace-pre-line">
          {summary.text}
        </p>
      )}
      <Dialog
        open={confirming}
        onOpenChange={(value) => {
          if (!remove.isPending) setConfirming(value)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete this summary?</DialogTitle>
            <DialogDescription>
              A later run in this thread can write a new summary.
            </DialogDescription>
          </DialogHeader>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <DialogFooter>
            <Button
              variant="outline"
              disabled={remove.isPending}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={() => void confirm()}
            >
              {remove.isPending ? "Deleting…" : "Delete summary"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </li>
  )
}

function Summaries({ workspace }: { workspace: Workspace }) {
  const [draft, setDraft] = useState("")
  const [search, setSearch] = useState("")
  const searchInput = useRef<HTMLInputElement>(null)
  const query = useThreadSummaries(workspace.id, search)
  const summaries = query.data?.pages.flatMap((page) => page.summaries) ?? []

  function submit(event: FormEvent) {
    event.preventDefault()
    setSearch(draft.trim())
  }

  function clear() {
    setDraft("")
    setSearch("")
    searchInput.current?.focus()
  }

  return (
    <>
      <PageHeading
        title="Memory"
        description="Hondo keeps a short summary of each channel thread it takes part in, and searches them in later threads."
      />
      <form role="search" onSubmit={submit} className="sm:max-w-sm">
        <InputGroup>
          <InputGroupAddon>
            <RiSearchLine aria-hidden="true" />
          </InputGroupAddon>
          <InputGroupInput
            ref={searchInput}
            type="search"
            aria-label="Search summaries"
            placeholder="Search summaries…"
            maxLength={200}
            value={draft}
            onChange={(event) => {
              setDraft(event.target.value)
              if (!event.target.value.trim()) setSearch("")
            }}
          />
        </InputGroup>
      </form>
      {query.isPending ? (
        <Skeleton className="h-64 w-full" />
      ) : !query.data ? (
        <ErrorCard
          title="Could not load summaries"
          onRetry={() => void query.refetch()}
          retrying={query.isFetching}
        />
      ) : summaries.length === 0 ? (
        <Empty className="min-h-64 border border-solid">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              {search ? <RiSearchLine /> : <RiBrainLine />}
            </EmptyMedia>
            <EmptyTitle>
              {search ? "No summaries match your search" : "No summaries yet"}
            </EmptyTitle>
            <EmptyDescription>
              {search
                ? "Try the words people used in the thread."
                : "Hondo writes a summary after it takes part in a channel thread."}
            </EmptyDescription>
          </EmptyHeader>
          {search && (
            <EmptyContent>
              <Button variant="outline" onClick={clear}>
                Clear search
              </Button>
            </EmptyContent>
          )}
        </Empty>
      ) : (
        <div className="flex flex-col items-start gap-4">
          <ul
            aria-label="Thread summaries"
            className="w-full divide-y rounded-lg border"
          >
            {summaries.map((summary) => (
              <SummaryItem
                key={summary.id}
                workspaceId={workspace.id}
                summary={summary}
                now={query.dataUpdatedAt}
                returnFocus={searchInput}
              />
            ))}
          </ul>
          {query.hasNextPage && (
            <Button
              variant="outline"
              disabled={query.isFetchingNextPage}
              onClick={() => void query.fetchNextPage()}
            >
              {query.isFetchingNextPage ? "Loading…" : "Show more"}
            </Button>
          )}
        </div>
      )}
    </>
  )
}

export default function MemoryPage() {
  const { workspaceId } = useParams()
  const selection = useWorkspace()
  const { workspace } = selection

  if (selection.isPending) return <Skeleton className="h-64 w-full" />
  if (selection.isError || !workspace)
    return <ErrorCard title="Could not load your workspace" />
  if (!workspaceId) return <Navigate to={memoryPath(workspace.id)} replace />
  if (workspaceId !== workspace.id)
    return <ErrorCard title="Workspace not found" />
  return (
    <div key={workspace.id} className="flex w-full max-w-4xl flex-col gap-6">
      <Summaries workspace={workspace} />
    </div>
  )
}
