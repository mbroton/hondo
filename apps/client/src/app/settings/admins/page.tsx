import { useId, useRef, useState, type RefObject } from "react"
import { RiUserAddLine } from "@remixicon/react"
import { toast } from "sonner"

import { ErrorCard } from "@/components/error-card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Combobox,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
  ComboboxList,
} from "@/components/ui/combobox"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import { ApiError } from "@/lib/api/client"
import {
  useAddAdmin,
  useAdmins,
  useRemoveAdmin,
  useSlackMembers,
} from "@/lib/api/queries"
import type { Admin, SlackMember } from "@/lib/api/types"
import { initials } from "@/lib/format"

const addedFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" })

function adminError(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.detail === "member_unavailable")
      return "This person can't be an admin. They are a guest, a bot, or no longer in this Slack workspace."
    if (error.detail === "unavailable" || error.status === 429)
      return "Slack didn't answer. Try again in a minute."
    if (error.detail === "last_admin")
      return "Add another admin before you remove the last one."
    if (error.status === 401) return "Your session has expired. Sign in again."
  }
  return fallback
}

function personInitials(name: string): string {
  const parts = name.trim().split(/\s+/)
  return (
    initials(parts[0] ?? "", parts.length > 1 ? parts[parts.length - 1] : "") ||
    "?"
  )
}

function PersonAvatar({
  name,
  avatarUrl,
  size = "default",
}: {
  name: string
  avatarUrl: string | null
  size?: "default" | "sm"
}) {
  return (
    <Avatar size={size}>
      {avatarUrl && <AvatarImage src={avatarUrl} alt="" />}
      <AvatarFallback>{personInitials(name)}</AvatarFallback>
    </Avatar>
  )
}

function AdminRow({
  workspaceId,
  admin,
  onlyAdmin,
  returnFocus,
}: {
  workspaceId: string
  admin: Admin
  onlyAdmin: boolean
  returnFocus: RefObject<HTMLButtonElement | null>
}) {
  const [confirming, setConfirming] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const remove = useRemoveAdmin(workspaceId)
  const label = admin.name ?? admin.slack_user_id
  const added = addedFormat.format(new Date(admin.created_at))

  async function confirm() {
    setError(null)
    try {
      await remove.mutateAsync(admin.slack_user_id)
      toast.success(`${label} was removed from the admin list`)
      setConfirming(false)
      requestAnimationFrame(() => returnFocus.current?.focus())
    } catch (cause) {
      setError(adminError(cause, "The admin could not be removed. Try again."))
    }
  }

  return (
    <li className="flex items-center gap-3 px-4 py-3">
      <PersonAvatar name={label} avatarUrl={admin.avatar_url} />
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="truncate text-sm font-medium" title={label}>
            {label}
          </span>
          {admin.status === "inactive" && (
            <Badge variant="outline">No access</Badge>
          )}
          {admin.status === "unknown" && (
            <Badge variant="outline">Slack didn’t answer</Badge>
          )}
        </div>
        <span className="truncate text-xs text-muted-foreground">
          {admin.added_by
            ? `Added by ${admin.added_by} · ${added}`
            : `Added ${added}`}
        </span>
      </div>
      <Button
        variant="ghost"
        size="sm"
        disabled={onlyAdmin || remove.isPending}
        aria-label={`Remove ${label}`}
        onClick={() => {
          setError(null)
          setConfirming(true)
        }}
      >
        Remove
      </Button>
      <Dialog
        open={confirming}
        onOpenChange={(value) => {
          if (!remove.isPending) setConfirming(value)
        }}
      >
        <DialogContent finalFocus={returnFocus}>
          <DialogHeader>
            <DialogTitle>Remove {label} as an admin?</DialogTitle>
            <DialogDescription>
              They can’t open this dashboard anymore, unless they are a Slack
              workspace owner or admin. They can still use Hondo in Slack.
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
              {remove.isPending ? "Removing…" : "Remove admin"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </li>
  )
}

function AddAdminDialog({
  workspaceId,
  admins,
  returnFocus,
  onClose,
}: {
  workspaceId: string
  admins: Admin[]
  returnFocus: RefObject<HTMLButtonElement | null>
  onClose: () => void
}) {
  const id = useId()
  const [error, setError] = useState<string | null>(null)
  const members = useSlackMembers(workspaceId)
  const add = useAddAdmin(workspaceId)
  const listed = new Set(admins.map((admin) => admin.slack_user_id))
  const loaded = members.data?.pages.flatMap((page) => page.members) ?? []
  const candidates = loaded.filter(
    (member) => !listed.has(member.slack_user_id)
  )
  const rateLimited =
    members.isFetching &&
    members.failureReason instanceof ApiError &&
    members.failureReason.status === 429

  async function choose(member: SlackMember | null) {
    if (!member) return
    setError(null)
    try {
      await add.mutateAsync(member.slack_user_id)
      toast.success(`${member.name} is now an admin`)
      onClose()
    } catch (cause) {
      setError(adminError(cause, "The admin could not be added. Try again."))
    }
  }

  return (
    <Dialog
      open
      onOpenChange={(value) => {
        if (!value && !add.isPending) onClose()
      }}
    >
      <DialogContent
        finalFocus={returnFocus}
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto"
      >
        <DialogHeader>
          <DialogTitle>Add an admin</DialogTitle>
          <DialogDescription>
            Pick a person from your Slack workspace. They can open this
            dashboard the next time they sign in with Slack.
          </DialogDescription>
        </DialogHeader>
        <Field>
          <FieldLabel htmlFor={`${id}-person`}>Person</FieldLabel>
          <Combobox<SlackMember>
            items={candidates}
            value={null}
            onValueChange={(member: SlackMember | null) => void choose(member)}
            itemToStringLabel={(member) => member.name}
            isItemEqualToValue={(item, current) =>
              item.slack_user_id === current.slack_user_id
            }
            filter={(member, search) =>
              [member.name, member.real_name, member.handle].some((text) =>
                text?.toLowerCase().includes(search.toLowerCase())
              )
            }
            disabled={add.isPending}
          >
            <ComboboxInput
              id={`${id}-person`}
              autoFocus
              disabled={add.isPending}
              placeholder={
                members.hasNextPage ? "Search loaded people…" : "Search people…"
              }
              aria-describedby={`${id}-help`}
            />
            <ComboboxContent>
              <ComboboxEmpty>
                {members.isPending ? "Loading people…" : "No matching people."}
              </ComboboxEmpty>
              <ComboboxList>
                {(member: SlackMember) => (
                  <ComboboxItem key={member.slack_user_id} value={member}>
                    <span className="flex min-w-0 items-center gap-2">
                      <PersonAvatar
                        name={member.name}
                        avatarUrl={member.avatar_url}
                        size="sm"
                      />
                      <span className="truncate">{member.name}</span>
                      {member.real_name && member.real_name !== member.name && (
                        <span className="truncate text-muted-foreground">
                          {member.real_name}
                        </span>
                      )}
                    </span>
                  </ComboboxItem>
                )}
              </ComboboxList>
            </ComboboxContent>
          </Combobox>
          <FieldDescription id={`${id}-help`}>
            Guests and bots can’t be admins. Slack workspace owners and admins
            already have access.
          </FieldDescription>
        </Field>
        {members.isError && (
          <Alert>
            <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
              {adminError(members.error, "Could not load people. Try again.")}
              <Button
                variant="outline"
                size="sm"
                disabled={members.isFetching}
                onClick={() =>
                  void (members.isFetchNextPageError
                    ? members.fetchNextPage()
                    : members.refetch())
                }
              >
                Try again
              </Button>
            </AlertDescription>
          </Alert>
        )}
        {rateLimited && (
          <p className="text-sm text-muted-foreground" role="status">
            Slack is limiting requests. Hondo tries again soon.
          </p>
        )}
        {members.hasNextPage && !members.isError && (
          <div className="flex flex-wrap items-center justify-between gap-3">
            <p className="text-sm text-muted-foreground">
              {loaded.length} people loaded. More are in this Slack workspace.
            </p>
            <Button
              variant="outline"
              size="sm"
              disabled={members.isFetching}
              onClick={() => void members.fetchNextPage()}
            >
              {members.isFetchingNextPage
                ? "Loading people…"
                : "Load more people"}
            </Button>
          </div>
        )}
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button variant="outline" disabled={add.isPending} onClick={onClose}>
            {add.isPending ? "Adding…" : "Cancel"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function WorkspaceAdmins({ workspaceId }: { workspaceId: string }) {
  const query = useAdmins(workspaceId)
  const [adding, setAdding] = useState(false)
  const addButton = useRef<HTMLButtonElement>(null)
  if (query.isPending) return <Skeleton className="h-40 w-full" />
  if (query.isError)
    return (
      <ErrorCard
        title="Could not load the admins"
        onRetry={() => void query.refetch()}
        retrying={query.isFetching}
      />
    )
  const admins = query.data
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="overflow-hidden rounded-xl border">
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3">
          <p className="text-sm font-medium">
            Admins{" "}
            <span className="ml-1 text-muted-foreground">{admins.length}</span>
          </p>
          <Button
            ref={addButton}
            variant="outline"
            size="sm"
            onClick={() => setAdding(true)}
          >
            <RiUserAddLine data-icon="inline-start" />
            Add admin
          </Button>
        </div>
        <ul className="divide-y" aria-label="Admins">
          {admins.map((admin) => (
            <AdminRow
              key={admin.slack_user_id}
              workspaceId={workspaceId}
              admin={admin}
              onlyAdmin={admins.length === 1}
              returnFocus={addButton}
            />
          ))}
        </ul>
      </div>
      {admins.length === 1 && (
        <p className="text-xs text-muted-foreground">
          Add another admin before you remove this one.
        </p>
      )}
      {adding && (
        <AddAdminDialog
          workspaceId={workspaceId}
          admins={admins}
          returnFocus={addButton}
          onClose={() => setAdding(false)}
        />
      )}
    </div>
  )
}
