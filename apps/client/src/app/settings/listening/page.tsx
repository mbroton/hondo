import { RiDeleteBinLine } from "@remixicon/react"
import { useId, useState } from "react"
import { toast } from "sonner"

import { ErrorCard } from "@/components/error-card"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import {
  Combobox,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
  ComboboxList,
} from "@/components/ui/combobox"
import { Field, FieldLabel } from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { useMCPChannels } from "@/lib/api/mcp"
import { ApiError } from "@/lib/api/client"
import {
  useListening,
  useMentionOnly,
  useSaveListening,
} from "@/lib/api/queries"
import type { ListeningChannels } from "@/lib/api/types"

const MAX_GOAL_LENGTH = 1000

type Channel = { id: string; name: string }
type Row = ListeningChannels["channels"][number]

function sameRows(a: Row[], b: Row[]) {
  return (
    a.length === b.length &&
    a.every((row) =>
      b.some((item) => item.channel === row.channel && item.goal === row.goal)
    )
  )
}

function ListeningForm({
  workspaceId,
  saved,
}: {
  workspaceId: string
  saved: ListeningChannels
}) {
  const id = useId()
  const [rows, setRows] = useState(saved.channels)
  const action = useSaveListening(workspaceId)
  const channelQuery = useMCPChannels(workspaceId)
  const mentionOnly = useMentionOnly(workspaceId).data?.channels ?? []
  const known = new Map(
    (channelQuery.data?.pages.flatMap((page) => page.channels) ?? []).map(
      (channel) => [channel.id, channel.name]
    )
  )
  const candidates: Channel[] = [...known]
    .filter(
      ([channelId]) =>
        !rows.some((row) => row.channel === channelId) &&
        !mentionOnly.includes(channelId)
    )
    .map(([channelId, name]) => ({ id: channelId, name }))
    .toSorted((a, b) => a.name.localeCompare(b.name))
  const trimmed = rows.map((row) => ({ ...row, goal: row.goal.trim() }))
  const valid = trimmed.every((row) => row.goal.length > 0)
  const changed = !sameRows(trimmed, saved.channels)

  function channelName(channelId: string) {
    const name = known.get(channelId)
    return name ? `#${name}` : `Saved channel (${channelId})`
  }

  function setGoal(channelId: string, goal: string) {
    setRows(
      rows.map((row) => (row.channel === channelId ? { ...row, goal } : row))
    )
  }

  async function save() {
    try {
      await action.mutateAsync({ channels: trimmed })
      toast.success("Listening channels saved.")
    } catch (error) {
      toast.error(
        error instanceof ApiError && error.detail === "channel_conflict"
          ? "A mention-only channel cannot be a listening channel."
          : "The listening channels could not be saved. Try again."
      )
    }
  }

  return (
    <form
      aria-label="Listening channels"
      className="flex flex-col gap-6"
      onSubmit={(event) => {
        event.preventDefault()
        void save()
      }}
    >
      {rows.map((row) => (
        <Field key={row.channel}>
          <div className="flex items-center justify-between gap-3">
            <FieldLabel
              htmlFor={`${id}-${row.channel}`}
              className="wrap-anywhere"
            >
              {channelName(row.channel)}
            </FieldLabel>
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={`Stop listening in ${channelName(row.channel)}`}
              onClick={() =>
                setRows(rows.filter((item) => item.channel !== row.channel))
              }
              disabled={action.isPending}
            >
              <RiDeleteBinLine />
            </Button>
          </div>
          <Textarea
            id={`${id}-${row.channel}`}
            required
            maxLength={MAX_GOAL_LENGTH}
            placeholder="Answer questions about deploys, and link the runbook. Not social chat or questions for a named person."
            value={row.goal}
            onChange={(event) => setGoal(row.channel, event.target.value)}
            disabled={action.isPending}
          />
        </Field>
      ))}
      <Field>
        <FieldLabel htmlFor={`${id}-add`}>Add a channel</FieldLabel>
        <Combobox<Channel>
          items={candidates}
          value={null}
          onValueChange={(channel: Channel | null) => {
            if (channel) setRows([...rows, { channel: channel.id, goal: "" }])
          }}
          itemToStringLabel={(channel) => `#${channel.name}`}
          isItemEqualToValue={(item, current) => item.id === current.id}
          filter={(channel, search) =>
            channel.name.toLowerCase().includes(search.toLowerCase())
          }
          disabled={action.isPending}
        >
          <ComboboxInput
            id={`${id}-add`}
            disabled={action.isPending}
            placeholder={
              channelQuery.hasNextPage
                ? "Search loaded channels…"
                : "Search channels…"
            }
          />
          <ComboboxContent>
            <ComboboxEmpty>
              {channelQuery.isPending
                ? "Loading channels…"
                : "No matching channels. Hondo must be a member of the channel."}
            </ComboboxEmpty>
            <ComboboxList>
              {(channel: Channel) => (
                <ComboboxItem key={channel.id} value={channel}>
                  #{channel.name}
                </ComboboxItem>
              )}
            </ComboboxList>
          </ComboboxContent>
        </Combobox>
        {channelQuery.hasNextPage && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="w-fit"
            onClick={() => void channelQuery.fetchNextPage()}
            disabled={channelQuery.isFetching}
          >
            {channelQuery.isFetchingNextPage
              ? "Loading channels…"
              : "Load more channels"}
          </Button>
        )}
      </Field>
      <Button
        type="submit"
        className="w-fit"
        disabled={action.isPending || !changed || !valid}
      >
        {action.isPending ? "Saving…" : "Save listening channels"}
      </Button>
    </form>
  )
}

export function ListeningSettings({ workspaceId }: { workspaceId: string }) {
  const query = useListening(workspaceId)
  if (query.isPending) return <Skeleton className="h-48 w-full" />
  if (query.isError)
    return (
      <ErrorCard
        title="Could not load the listening channels"
        onRetry={() => void query.refetch()}
        retrying={query.isFetching}
      />
    )
  return (
    <Card>
      <CardContent>
        <ListeningForm
          key={JSON.stringify(query.data.channels)}
          workspaceId={workspaceId}
          saved={query.data}
        />
      </CardContent>
    </Card>
  )
}
