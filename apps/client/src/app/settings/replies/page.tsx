import { useState } from "react"
import { toast } from "sonner"

import { ChannelPicker } from "@/components/channel-picker"
import { ErrorCard } from "@/components/error-card"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError } from "@/lib/api/client"
import {
  useListening,
  useMentionOnly,
  useSaveMentionOnly,
} from "@/lib/api/queries"
import type { MentionOnlyChannels } from "@/lib/api/types"

function sameChannels(a: string[], b: string[]) {
  return a.length === b.length && a.every((channel) => b.includes(channel))
}

function MentionOnlyForm({
  workspaceId,
  saved,
}: {
  workspaceId: string
  saved: MentionOnlyChannels
}) {
  const [channels, setChannels] = useState(saved.channels)
  const action = useSaveMentionOnly(workspaceId)
  const listening = useListening(workspaceId)
  const changed = !sameChannels(channels, saved.channels)

  async function save() {
    try {
      await action.mutateAsync({ channels })
      toast.success("Channels saved.")
    } catch (error) {
      toast.error(
        error instanceof ApiError && error.detail === "channel_conflict"
          ? "A listening channel cannot be mention-only."
          : "The channels could not be saved. Try again."
      )
    }
  }

  return (
    <form
      aria-label="Mention-only channels"
      className="flex flex-col gap-6"
      onSubmit={(event) => {
        event.preventDefault()
        void save()
      }}
    >
      <ChannelPicker
        workspaceId={workspaceId}
        legend="Mention-only channels"
        channels={channels}
        savedChannels={saved.channels}
        onChannelsChange={setChannels}
        hiddenChannels={
          listening.data?.channels.map((row) => row.channel) ?? []
        }
        disabled={action.isPending}
      />
      <Button
        type="submit"
        className="w-fit"
        disabled={action.isPending || !changed}
      >
        {action.isPending ? "Saving…" : "Save channels"}
      </Button>
    </form>
  )
}

export function MentionOnlySettings({ workspaceId }: { workspaceId: string }) {
  const query = useMentionOnly(workspaceId)
  if (query.isPending) return <Skeleton className="h-48 w-full" />
  if (query.isError)
    return (
      <ErrorCard
        title="Could not load the channels"
        onRetry={() => void query.refetch()}
        retrying={query.isFetching}
      />
    )
  return (
    <Card>
      <CardContent>
        <MentionOnlyForm
          key={query.data.channels.join(",")}
          workspaceId={workspaceId}
          saved={query.data}
        />
      </CardContent>
    </Card>
  )
}
