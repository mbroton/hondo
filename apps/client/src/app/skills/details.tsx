import { useId, useState } from "react"
import { Link, useNavigate, useParams } from "react-router"
import {
  RiArrowLeftLine,
  RiDeleteBinLine,
  RiExternalLinkLine,
} from "@remixicon/react"
import { toast } from "sonner"
import { ChannelPicker } from "@/components/channel-picker"
import { ErrorCard } from "@/components/error-card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { ApiError } from "@/lib/api/client"
import { needsSlackReinstall, useMCPChannels } from "@/lib/api/mcp"
import { useSkill, useSkillAction } from "@/lib/api/skills"
import type { Skill } from "@/lib/api/types"
import { skillsPath, useSkillsPage } from "./context"
import { skillErrorMessage } from "./helpers"
import { SkillFile } from "./skill-file"

function BackLink({ workspaceId }: { workspaceId: string }) {
  return (
    <Link
      to={skillsPath(workspaceId)}
      className="flex w-fit items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
    >
      <RiArrowLeftLine className="size-4" aria-hidden="true" />
      Skills
    </Link>
  )
}

function SkillDetails({ skill }: { skill: Skill }) {
  const { workspace } = useSkillsPage()
  const navigate = useNavigate()
  const id = useId()
  const query = useSkill(workspace.id, skill.id)
  const action = useSkillAction(workspace.id)
  const channelQuery = useMCPChannels(workspace.id)
  const [saved, setSaved] = useState(skill)
  const latest = skill.revision >= saved.revision ? skill : saved
  const [content, setContent] = useState(saved.content)
  const [published, setPublished] = useState(saved.published)
  const [allChannels, setAllChannels] = useState(saved.all_channels)
  const [channels, setChannels] = useState(saved.channels)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const changed =
    content !== saved.content ||
    published !== saved.published ||
    allChannels !== saved.all_channels ||
    channels.toSorted().join() !== saved.channels.toSorted().join()
  const missingChannels = published && !allChannels && channels.length === 0

  function reset(next: Skill) {
    setSaved(next)
    setContent(next.content)
    setPublished(next.published)
    setAllChannels(next.all_channels)
    setChannels(next.channels)
    setConfirmDelete(false)
    action.reset()
  }

  async function save() {
    if (!changed || missingChannels || action.isPending) return
    try {
      const result = await action.mutateAsync({
        type: "save",
        id: saved.id,
        body: {
          revision: saved.revision,
          content,
          published,
          all_channels: allChannels,
          channels: allChannels ? [] : channels,
        },
      })
      if (result) reset(result)
      toast.success("Skill saved.")
    } catch (cause) {
      toast.error(skillErrorMessage(cause))
    }
  }

  async function remove() {
    try {
      await action.mutateAsync({ type: "delete", id: saved.id })
      navigate(skillsPath(workspace.id), { replace: true })
      toast.success("Skill deleted.")
    } catch (cause) {
      toast.error(skillErrorMessage(cause))
    }
  }

  return (
    <>
      <BackLink workspaceId={workspace.id} />
      <title>{`${saved.name} · Hondo`}</title>
      <div className="flex flex-col items-start gap-4 sm:flex-row sm:justify-between">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <h1 className="text-2xl font-semibold tracking-tight wrap-anywhere sm:text-3xl">
            {saved.name}
          </h1>
          <p className="max-w-2xl text-sm leading-6 wrap-anywhere text-muted-foreground">
            {saved.description}
          </p>
          {latest.source_url && (
            <a
              href={latest.source_url}
              target="_blank"
              rel="noreferrer"
              className="flex w-fit items-center gap-1.5 text-sm underline-offset-4 hover:underline"
            >
              Drafted from this thread
              <RiExternalLinkLine
                className="size-4 text-muted-foreground"
                aria-hidden="true"
              />
            </a>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-4">
          <Field orientation="horizontal" className="w-auto">
            <Switch
              id={`${id}-published`}
              aria-label="Publish skill"
              checked={published}
              onCheckedChange={setPublished}
              disabled={action.isPending}
            />
            <FieldLabel htmlFor={`${id}-published`}>
              <span className="sr-only">Publish skill</span>
              <span aria-hidden="true">
                {published ? "Published" : "Draft"}
              </span>
            </FieldLabel>
          </Field>
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label="Delete skill"
            onClick={() => setConfirmDelete(true)}
            disabled={action.isPending}
          >
            <RiDeleteBinLine data-icon="inline-start" />
            Delete
          </Button>
        </div>
      </div>
      {confirmDelete && (
        <Alert variant="destructive">
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>
              Delete this skill? Hondo stops using it in all channels.
            </span>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setConfirmDelete(false)}
                disabled={action.isPending}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={() => void remove()}
                disabled={action.isPending}
              >
                Confirm deletion
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}
      {needsSlackReinstall(channelQuery.error) && (
        <Alert>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>
              Hondo needs more Slack permissions to list channels. Reinstall
              Hondo in Slack from workspace settings.
            </span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void channelQuery.refetch()}
              disabled={channelQuery.isFetching}
            >
              Check again
            </Button>
          </AlertDescription>
        </Alert>
      )}
      {query.isError && (
        <Alert>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            {query.error instanceof ApiError && query.error.status === 404 ? (
              skillErrorMessage(query.error)
            ) : (
              <>
                <span>
                  Could not refresh this skill. Your unsaved changes are kept.
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={query.isFetching}
                  onClick={() => void query.refetch()}
                >
                  {query.isFetching ? "Retrying..." : "Retry"}
                </Button>
              </>
            )}
          </AlertDescription>
        </Alert>
      )}
      {action.isError && (
        <Alert variant="destructive">
          <AlertDescription>{skillErrorMessage(action.error)}</AlertDescription>
        </Alert>
      )}
      {latest.revision > saved.revision && (
        <Alert>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>This skill changed. Your edits are still here.</span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => reset(latest)}
              disabled={action.isPending}
            >
              {changed ? "Discard changes" : "Load latest version"}
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <div className="flex min-w-0 flex-col gap-6">
        <FieldGroup className="max-w-3xl gap-8">
          <SkillFile
            id={`${id}-content`}
            value={content}
            onChange={setContent}
            disabled={action.isPending}
          />
          <ChannelPicker
            workspaceId={workspace.id}
            description="Select where Hondo can use this skill. Direct messages are excluded."
            allChannels={allChannels}
            onAllChannelsChange={setAllChannels}
            channels={channels}
            savedChannels={saved.channels}
            onChannelsChange={setChannels}
            disabled={action.isPending}
          />
        </FieldGroup>
        {changed && (
          <div className="sticky bottom-3 z-10 flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-background p-3 shadow-sm">
            <p className="text-sm">
              {missingChannels
                ? "Select at least one channel to publish this skill."
                : "Unsaved changes"}
            </p>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => reset(latest)}
                disabled={action.isPending}
              >
                Discard changes
              </Button>
              <Button
                type="button"
                onClick={() => void save()}
                disabled={
                  action.isPending || missingChannels || !content.trim()
                }
              >
                {action.isPending ? "Saving…" : "Save changes"}
              </Button>
            </div>
          </div>
        )}
      </div>
    </>
  )
}

export default function SkillDetailsPage() {
  const { skillId = "" } = useParams()
  const { workspace } = useSkillsPage()
  const query = useSkill(workspace.id, skillId)
  if (query.isPending) return <Skeleton className="h-64 w-full" />
  if (!query.data) {
    const missing =
      query.error instanceof ApiError && query.error.status === 404
    return (
      <>
        <BackLink workspaceId={workspace.id} />
        <ErrorCard
          title={missing ? "Skill not found" : "Could not load this skill"}
          description={
            missing
              ? "This skill no longer exists."
              : "This information could not be loaded. Try again."
          }
          onRetry={() => void query.refetch()}
          retrying={query.isFetching}
        />
      </>
    )
  }
  return <SkillDetails key={query.data.id} skill={query.data} />
}
