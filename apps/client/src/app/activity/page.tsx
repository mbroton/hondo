import { useState } from "react"
import {
  RiChat3Line,
  RiExternalLinkLine,
  RiHashtag,
  RiRefreshLine,
  RiSearchLine,
  RiTimeLine,
} from "@remixicon/react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
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
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ErrorCard } from "@/components/error-card"
import { PageHeading } from "@/components/page-heading"
import { useWorkspace } from "@/hooks/use-workspace"
import { useActivity } from "@/lib/api/queries"
import { formatRelativeTime } from "@/lib/format"

const states: Record<
  string,
  { label: string; variant: "default" | "secondary" | "outline" }
> = {
  running: { label: "Running", variant: "default" },
  paused_on_approval: { label: "Needs approval", variant: "secondary" },
  idle: { label: "Idle", variant: "outline" },
}
const filters = [
  { value: "all", label: "All statuses" },
  ...Object.entries(states).map(([value, state]) => ({
    value,
    label: state.label,
  })),
]

function StateBadge({ state }: { state: string }) {
  return (
    <Badge variant={states[state]?.variant ?? "outline"}>
      {state === "running" && (
        <span className="size-1.5 rounded-full bg-current" aria-hidden="true" />
      )}
      {state === "paused_on_approval" && (
        <RiTimeLine data-icon="inline-start" />
      )}
      {states[state]?.label ?? state}
    </Badge>
  )
}

export default function ActivityPage() {
  const { workspace, isPending, isError } = useWorkspace()
  const workspaceId = workspace?.id ?? null
  const activity = useActivity(workspaceId)
  const [search, setSearch] = useState("")
  const [status, setStatus] = useState("all")
  const items = activity.data ?? []
  const visible = items.filter(
    (item) =>
      (status === "all" || status === item.state) &&
      `${item.channel} ${item.thread_root}`
        .toLowerCase()
        .includes(search.trim().toLowerCase())
  )
  const loading = isPending || activity.isPending
  const filtered = status !== "all" || search.trim() !== ""

  return (
    <div className="flex flex-col gap-7">
      <PageHeading
        title="Activity"
        description="Follow Hondo’s conversations across your Slack workspace."
      >
        <Button
          variant="outline"
          disabled={!workspaceId || activity.isFetching}
          onClick={() => void activity.refetch()}
        >
          <RiRefreshLine data-icon="inline-start" />
          {activity.isFetching ? "Refreshing…" : "Refresh"}
        </Button>
      </PageHeading>
      {isError || (!isPending && !workspace) ? (
        <ErrorCard title="Could not load your workspace" />
      ) : activity.isError ? (
        <ErrorCard
          title="Could not load recent activity"
          onRetry={() => void activity.refetch()}
          retrying={activity.isFetching}
        />
      ) : (
        <Card className="gap-0 pb-0">
          <CardHeader className="gap-5 pb-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div className="flex flex-col gap-1">
                <CardTitle>Recent conversations</CardTitle>
                <CardDescription>
                  Up to 50 conversations, updated every 30 seconds.
                </CardDescription>
              </div>
              {!loading && (
                <Badge variant="outline">
                  {items.length}{" "}
                  {items.length === 1 ? "conversation" : "conversations"}
                </Badge>
              )}
            </div>
            <div className="flex flex-col gap-3 sm:flex-row">
              <InputGroup className="sm:max-w-xs">
                <InputGroupAddon>
                  <RiSearchLine aria-hidden="true" />
                </InputGroupAddon>
                <InputGroupInput
                  aria-label="Search conversations"
                  placeholder="Search channel or thread ID…"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                />
              </InputGroup>
              <Select
                value={status}
                onValueChange={(value) => {
                  if (value) setStatus(value)
                }}
              >
                <SelectTrigger
                  aria-label="Filter by status"
                  className="w-full sm:w-44"
                >
                  <SelectValue>
                    {filters.find((filter) => filter.value === status)?.label}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {filters.map((filter) => (
                      <SelectItem key={filter.value} value={filter.value}>
                        {filter.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </div>
          </CardHeader>
          {!loading && !visible.length ? (
            <Empty className="min-h-80 border-t">
              <EmptyHeader>
                <EmptyMedia variant="icon">
                  {filtered ? <RiSearchLine /> : <RiChat3Line />}
                </EmptyMedia>
                <EmptyTitle>
                  {filtered
                    ? "No matching conversations"
                    : "Your next conversation starts in Slack"}
                </EmptyTitle>
                <EmptyDescription>
                  {filtered
                    ? "Try another channel or thread ID, or clear the filters."
                    : "Invite Hondo to a channel and mention @Hondo with a request. Its activity will appear here."}
                </EmptyDescription>
              </EmptyHeader>
              {filtered && (
                <EmptyContent>
                  <Button
                    variant="outline"
                    onClick={() => {
                      setSearch("")
                      setStatus("all")
                    }}
                  >
                    Clear filters
                  </Button>
                </EmptyContent>
              )}
            </Empty>
          ) : (
            <Table aria-label="Recent conversations" aria-busy={loading}>
              <TableHeader>
                <TableRow className="bg-muted/40">
                  <TableHead>Conversation</TableHead>
                  <TableHead className="hidden @xl/main:table-cell">
                    Status
                  </TableHead>
                  <TableHead className="hidden @3xl/main:table-cell">
                    Last active
                  </TableHead>
                  <TableHead className="text-right">
                    <span className="sr-only">Open in Slack</span>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loading
                  ? Array.from({ length: 4 }, (_, index) => (
                      <TableRow key={index}>
                        {Array.from({ length: 4 }, (_, cell) => (
                          <TableCell
                            key={cell}
                            className={
                              cell === 1
                                ? "hidden @xl/main:table-cell"
                                : cell === 2
                                  ? "hidden @3xl/main:table-cell"
                                  : undefined
                            }
                          >
                            <Skeleton className="h-5 min-w-16" />
                          </TableCell>
                        ))}
                      </TableRow>
                    ))
                  : visible.map((item) => (
                      <TableRow key={`${item.channel}/${item.thread_root}`}>
                        <TableCell>
                          <div className="flex items-center gap-3">
                            <div className="hidden size-9 shrink-0 items-center justify-center rounded-lg border bg-muted/40 @xl/main:flex">
                              <RiHashtag
                                className="size-4 text-muted-foreground"
                                aria-hidden="true"
                              />
                            </div>
                            <div className="flex flex-col gap-1">
                              <span className="font-medium">
                                {item.channel}
                              </span>
                              <span className="text-xs text-muted-foreground">
                                Thread {item.thread_root}
                              </span>
                              <span className="hidden text-xs text-muted-foreground @xl/main:inline">
                                Started{" "}
                                {new Date(item.created_at).toLocaleString(
                                  undefined,
                                  {
                                    month: "short",
                                    day: "numeric",
                                    hour: "numeric",
                                    minute: "2-digit",
                                  }
                                )}
                              </span>
                              <div className="flex flex-wrap items-center gap-2 @xl/main:hidden">
                                <StateBadge state={item.state} />
                                <time
                                  className="text-xs text-muted-foreground"
                                  dateTime={item.updated_at}
                                >
                                  {formatRelativeTime(
                                    item.updated_at,
                                    activity.dataUpdatedAt
                                  )}
                                </time>
                              </div>
                            </div>
                          </div>
                        </TableCell>
                        <TableCell className="hidden @xl/main:table-cell">
                          <StateBadge state={item.state} />
                        </TableCell>
                        <TableCell className="hidden text-muted-foreground @3xl/main:table-cell">
                          <time
                            dateTime={item.updated_at}
                            title={new Date(item.updated_at).toLocaleString()}
                          >
                            {formatRelativeTime(
                              item.updated_at,
                              activity.dataUpdatedAt
                            )}
                          </time>
                        </TableCell>
                        <TableCell className="text-right">
                          <Button
                            variant="ghost"
                            size="sm"
                            nativeButton={false}
                            render={
                              <a
                                href={`https://slack.com/app_redirect?team=${encodeURIComponent(workspace?.slack_team_id ?? "")}&channel=${encodeURIComponent(item.channel)}`}
                                target="_blank"
                                rel="noreferrer"
                                aria-label={`Open channel ${item.channel} in Slack`}
                              />
                            }
                          >
                            <span className="hidden @xl/main:inline">
                              Open channel
                            </span>
                            <RiExternalLinkLine data-icon="inline-end" />
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
              </TableBody>
            </Table>
          )}
          {!loading && visible.length > 0 && (
            <div className="border-t px-6 py-4 text-xs text-muted-foreground">
              Showing {visible.length} of {items.length} recent conversations.
            </div>
          )}
        </Card>
      )}
    </div>
  )
}
