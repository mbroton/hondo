import { useId, useState } from "react"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldContent,
  FieldDescription,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError } from "@/lib/api/client"
import { needsSlackReinstall, useMCPChannels } from "@/lib/api/mcp"

const MAX_CHANNELS = 500

function toggle(values: string[], value: string, checked: boolean) {
  return checked ? [...values, value] : values.filter((item) => item !== value)
}

export function ChannelPicker({
  workspaceId,
  legend = "Allowed channels",
  description,
  allChannels = false,
  onAllChannelsChange,
  channels,
  savedChannels,
  onChannelsChange,
  hiddenChannels = [],
  disabled,
}: {
  workspaceId: string
  legend?: string
  description?: string
  allChannels?: boolean
  onAllChannelsChange?: (allChannels: boolean) => void
  channels: string[]
  savedChannels: string[]
  onChannelsChange: (channels: string[]) => void
  hiddenChannels?: string[]
  disabled: boolean
}) {
  const id = useId()
  const [search, setSearch] = useState("")
  const channelQuery = useMCPChannels(workspaceId)
  const knownChannels = [
    ...new Map(
      (channelQuery.data?.pages.flatMap((page) => page.channels) ?? []).map(
        (channel) => [channel.id, channel]
      )
    ).values(),
  ].toSorted((a, b) => a.name.localeCompare(b.name))
  const visibleChannels = [
    ...knownChannels,
    ...[...new Set([...savedChannels, ...channels])]
      .filter((channel) => !knownChannels.some((item) => item.id === channel))
      .map((channel) => ({ id: channel, name: `Saved channel (${channel})` })),
  ]
    .filter(
      (channel) =>
        channels.includes(channel.id) || !hiddenChannels.includes(channel.id)
    )
    .filter((channel) =>
      `${channel.name} ${channel.id}`
        .toLowerCase()
        .includes(search.toLowerCase())
    )

  return (
    <FieldSet className="gap-5">
      <div>
        <FieldLegend>{legend}</FieldLegend>
        {description && <FieldDescription>{description}</FieldDescription>}
      </div>
      {onAllChannelsChange && (
        <Field orientation="horizontal">
          <Checkbox
            id={`${id}-all`}
            checked={allChannels}
            onCheckedChange={onAllChannelsChange}
            disabled={disabled}
          />
          <FieldContent>
            <FieldLabel htmlFor={`${id}-all`}>All Slack channels</FieldLabel>
            <FieldDescription>
              Includes new channels. Select channels that are shared with other
              organizations one by one.
            </FieldDescription>
          </FieldContent>
        </Field>
      )}
      {!allChannels && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-3">
            <Input
              className="max-w-sm"
              aria-label="Search channels"
              placeholder={
                channelQuery.hasNextPage
                  ? "Search loaded channels…"
                  : "Search channels…"
              }
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            <span className="text-sm text-muted-foreground">
              {channels.length} of {MAX_CHANNELS} selected
            </span>
          </div>
          {channelQuery.isPending && <Skeleton className="h-20" />}
          {channelQuery.isError && !needsSlackReinstall(channelQuery.error) && (
            <Alert>
              <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
                Could not load more channels. Try again.
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    void (channelQuery.isFetchNextPageError
                      ? channelQuery.fetchNextPage()
                      : channelQuery.refetch())
                  }
                  disabled={channelQuery.isFetching}
                >
                  Try again
                </Button>
              </AlertDescription>
            </Alert>
          )}
          {visibleChannels.length > 0 && (
            <div className="max-h-96 overflow-y-auto rounded-lg border">
              {visibleChannels.map((channel) => (
                <Field
                  orientation="horizontal"
                  key={channel.id}
                  className="border-b px-4 py-3 last:border-0"
                >
                  <Checkbox
                    id={`${id}-${channel.id}`}
                    checked={channels.includes(channel.id)}
                    onCheckedChange={(checked) =>
                      onChannelsChange(toggle(channels, channel.id, checked))
                    }
                    disabled={
                      disabled ||
                      (channels.length >= MAX_CHANNELS &&
                        !channels.includes(channel.id))
                    }
                  />
                  <FieldLabel
                    htmlFor={`${id}-${channel.id}`}
                    className="wrap-anywhere"
                  >
                    #{channel.name}
                  </FieldLabel>
                </Field>
              ))}
            </div>
          )}
          {visibleChannels.length === 0 &&
            !channelQuery.isFetching &&
            !channelQuery.isError && (
              <p className="text-sm text-muted-foreground">
                {search
                  ? "No channels match your search. The list shows only channels where Hondo is a member."
                  : "Hondo is not in a channel yet. Add Hondo to a channel in Slack."}
              </p>
            )}
          {channelQuery.hasNextPage && (
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="text-sm text-muted-foreground">
                {knownChannels.length} channels loaded. More channels are
                available.
              </p>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => void channelQuery.fetchNextPage()}
                disabled={channelQuery.isFetching}
              >
                {channelQuery.isFetchingNextPage
                  ? "Loading channels…"
                  : "Load more channels"}
              </Button>
            </div>
          )}
          {channelQuery.isFetching &&
            channelQuery.failureReason instanceof ApiError &&
            channelQuery.failureReason.status === 429 && (
              <p className="text-sm text-muted-foreground" role="status">
                Channel loading is paused. It will retry automatically.
              </p>
            )}
        </>
      )}
    </FieldSet>
  )
}
