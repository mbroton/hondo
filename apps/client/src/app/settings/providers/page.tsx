import { useId, useRef, useState, type RefObject } from "react"
import {
  RiAddLine,
  RiCheckboxCircleLine,
  RiExternalLinkLine,
  RiKey2Line,
  RiMoreLine,
} from "@remixicon/react"
import { toast } from "sonner"

import { ErrorCard } from "@/components/error-card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
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
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "@/components/ui/empty"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
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
import { ApiError } from "@/lib/api/client"
import { useConnectionAction, useProviders } from "@/lib/api/queries"
import type { Provider, ProviderConnection } from "@/lib/api/types"

function connectionError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.detail === "key_rejected")
      return "The key was not accepted. Check the key and your provider account access."
    if (error.detail === "connection_name_taken")
      return "This name is already in use. Enter a different name."
    if (error.detail === "connections_unavailable")
      return "Hondo can't read the saved key. Edit the connection and enter the key again."
    if (error.detail === "connection_not_found")
      return "This connection no longer exists. Refresh the page."
    if (error.detail === "connection_in_use")
      return "This connection is in use. Change or remove its model selections in Settings, then wait for active runs to finish."
    if (error.status === 401) return "Your session has expired. Sign in again."
    if (error.status === 422) return "Check the connection name and API key."
  }
  return "The request could not be completed. Try again."
}

function ConnectionDialog({
  workspaceId,
  providers,
  connection,
  returnFocus,
  existingNames = [],
  onClose,
}: {
  workspaceId: string
  providers: Provider[]
  connection?: ProviderConnection
  returnFocus: RefObject<HTMLButtonElement | null>
  existingNames?: string[]
  onClose: () => void
}) {
  const id = useId()
  const providerInput = useRef<HTMLButtonElement>(null)
  const nameInput = useRef<HTMLInputElement>(null)
  const [open, setOpen] = useState(true)
  function availableName(providerName: string) {
    let candidate = providerName
    let suffix = 2
    while (
      existingNames.some(
        (item) => item.toLowerCase() === candidate.toLowerCase()
      )
    ) {
      candidate = `${providerName} ${suffix++}`
    }
    return candidate
  }

  const [providerId, setProviderId] = useState<string | undefined>(
    connection?.provider ?? providers[0]?.id
  )
  const provider = providers.find((item) => item.id === providerId)
  const [name, setName] = useState(
    () => connection?.name ?? availableName(provider?.name ?? "")
  )
  const [apiKey, setApiKey] = useState("")
  const [error, setError] = useState<string | null>(null)
  const action = useConnectionAction(workspaceId)

  async function save() {
    if (!provider || !name.trim() || (!connection && !apiKey.trim())) return
    setError(null)
    try {
      await action.mutateAsync(
        connection
          ? {
              type: "update",
              id: connection.id,
              name: name.trim(),
              apiKey: apiKey.trim() || undefined,
            }
          : {
              type: "create",
              provider: provider.id,
              name: name.trim(),
              apiKey: apiKey.trim(),
            }
      )
      setApiKey("")
      toast.success(connection ? "Connection updated" : "Connection added")
      setOpen(false)
    } catch (cause) {
      setError(connectionError(cause))
    } finally {
      action.reset()
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(value) => {
        if (!action.isPending) setOpen(value)
      }}
      onOpenChangeComplete={(value) => {
        if (!value) onClose()
      }}
    >
      <DialogContent
        initialFocus={connection ? nameInput : providerInput}
        finalFocus={returnFocus}
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-md"
      >
        <DialogHeader>
          <DialogTitle>
            {connection ? "Edit connection" : "Add connection"}
          </DialogTitle>
          <DialogDescription>
            {connection
              ? "Change the name or replace the API key."
              : "Save a provider key for this workspace."}
          </DialogDescription>
        </DialogHeader>
        <form
          className="flex flex-col gap-6"
          onSubmit={(event) => {
            event.preventDefault()
            void save()
          }}
        >
          <FieldGroup>
            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            <Field>
              <FieldLabel htmlFor={`${id}-provider`}>Provider</FieldLabel>
              <Select
                items={providers.map((item) => ({
                  value: item.id,
                  label: item.name,
                }))}
                value={provider?.id ?? null}
                disabled={!!connection || action.isPending}
                onValueChange={(value) => {
                  const next = providers.find((item) => item.id === value)
                  if (
                    !name.trim() ||
                    name === availableName(provider?.name ?? "")
                  )
                    setName(availableName(next?.name ?? ""))
                  setProviderId(value ?? undefined)
                  setApiKey("")
                  setError(null)
                }}
              >
                <SelectTrigger
                  ref={providerInput}
                  id={`${id}-provider`}
                  className="w-full"
                >
                  <SelectValue placeholder="Select provider" />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {providers.map((item) => (
                      <SelectItem key={item.id} value={item.id}>
                        {item.name}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
              <Input
                id={`${id}-name`}
                ref={nameInput}
                required
                maxLength={100}
                value={name}
                onChange={(event) => setName(event.target.value)}
                disabled={action.isPending}
                autoComplete="off"
                aria-describedby={`${id}-name-help`}
              />
              <FieldDescription id={`${id}-name-help`}>
                Use a name you can find in workspace settings.
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor={`${id}-key`}>
                {connection ? "New API key" : "API key"}
              </FieldLabel>
              <Input
                id={`${id}-key`}
                type="password"
                required={!connection}
                maxLength={65536}
                value={apiKey}
                onChange={(event) => setApiKey(event.target.value)}
                disabled={action.isPending}
                autoComplete="new-password"
                spellCheck={false}
                placeholder={
                  connection
                    ? "Leave blank to keep the current key"
                    : "Paste your API key"
                }
                aria-describedby={`${id}-key-help`}
              />
              <FieldDescription id={`${id}-key-help`}>
                Keys are encrypted and never shown after saving.
                {provider && (
                  <>
                    {" "}
                    <a
                      href={provider.api_key_url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 underline underline-offset-4"
                    >
                      Get an API key
                      <RiExternalLinkLine className="size-3" />
                    </a>
                  </>
                )}
              </FieldDescription>
            </Field>
          </FieldGroup>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={action.isPending}
              onClick={() => setOpen(false)}
            >
              Cancel
            </Button>
            <Button
              type="submit"
              disabled={
                action.isPending ||
                !provider ||
                !name.trim() ||
                (!connection && !apiKey.trim())
              }
            >
              {action.isPending
                ? "Saving…"
                : connection
                  ? apiKey.trim()
                    ? "Check and save"
                    : "Save changes"
                  : "Check and add"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function ConnectionRow({
  workspaceId,
  connection,
  providers,
  removeReturnFocus,
}: {
  workspaceId: string
  connection: ProviderConnection
  providers: Provider[]
  removeReturnFocus: RefObject<HTMLButtonElement | null>
}) {
  const [editing, setEditing] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const action = useConnectionAction(workspaceId)
  const providerName =
    providers.find((item) => item.id === connection.provider)?.name ??
    connection.provider

  async function run(type: "check" | "delete") {
    setError(null)
    try {
      await action.mutateAsync({ type, id: connection.id })
      toast.success(
        type === "check" ? "API key verified" : "Connection removed"
      )
      setRemoving(false)
      if (type === "delete") {
        requestAnimationFrame(() => removeReturnFocus.current?.focus())
      }
    } catch (cause) {
      const message = connectionError(cause)
      if (type === "check") toast.error(message)
      else setError(message)
    } finally {
      action.reset()
    }
  }

  return (
    <TableRow>
      <TableCell className="min-w-0 py-4 pl-4">
        <span className="block truncate font-medium" title={connection.name}>
          {connection.name}
        </span>
        <span className="block truncate text-xs text-muted-foreground sm:hidden">
          {providerName}
        </span>
      </TableCell>
      <TableCell className="hidden text-muted-foreground sm:table-cell">
        {providerName}
      </TableCell>
      <TableCell className="w-32">
        <Badge
          variant={
            connection.check_status === "valid" ? "secondary" : "destructive"
          }
          title={`Last checked ${new Date(connection.checked_at).toLocaleString()}`}
        >
          {connection.check_status === "valid" && (
            <RiCheckboxCircleLine data-icon="inline-start" />
          )}
          {action.isPending
            ? "Checking…"
            : connection.check_status === "valid"
              ? "Verified"
              : "Check failed"}
        </Badge>
      </TableCell>
      <TableCell className="w-12 pr-3 text-right">
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <Button
                ref={trigger}
                variant="ghost"
                size="icon-sm"
                aria-label={`Actions for ${connection.name}`}
                disabled={action.isPending}
              />
            }
          >
            <RiMoreLine />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-44">
            <DropdownMenuGroup>
              <DropdownMenuItem onClick={() => setEditing(true)}>
                Edit connection
              </DropdownMenuItem>
              <DropdownMenuItem onClick={() => void run("check")}>
                Check key
              </DropdownMenuItem>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuItem
                variant="destructive"
                onClick={() => {
                  setError(null)
                  setRemoving(true)
                }}
              >
                Remove connection
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
        {editing && (
          <ConnectionDialog
            workspaceId={workspaceId}
            providers={providers}
            connection={connection}
            returnFocus={trigger}
            onClose={() => setEditing(false)}
          />
        )}
        <Dialog
          open={removing}
          onOpenChange={(value) => {
            if (!action.isPending) setRemoving(value)
          }}
        >
          <DialogContent
            finalFocus={trigger}
            className="max-h-[calc(100dvh-2rem)] overflow-y-auto"
          >
            <DialogHeader>
              <DialogTitle>Remove connection?</DialogTitle>
              <DialogDescription>
                Remove “{connection.name}” and its saved key. Add it again if
                you need it later.
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
                disabled={action.isPending}
                onClick={() => setRemoving(false)}
              >
                Cancel
              </Button>
              <Button
                variant="destructive"
                disabled={action.isPending}
                onClick={() => void run("delete")}
              >
                {action.isPending ? "Removing…" : "Remove connection"}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </TableCell>
    </TableRow>
  )
}

export function WorkspaceProviders({ workspaceId }: { workspaceId: string }) {
  const query = useProviders(workspaceId)
  const [adding, setAdding] = useState(false)
  const addButton = useRef<HTMLButtonElement>(null)
  if (query.isPending) return <Skeleton className="h-48 w-full" />
  if (query.isError)
    return (
      <ErrorCard
        title="Could not load connections"
        onRetry={() => void query.refetch()}
        retrying={query.isFetching}
      />
    )
  const { providers, connections } = query.data
  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="overflow-hidden rounded-xl border">
        <div className="flex items-center justify-between gap-3 border-b px-4 py-3">
          <p className="text-sm font-medium">
            Saved connections{" "}
            <span className="ml-1 text-muted-foreground">
              {connections.length}
            </span>
          </p>
          <Button
            ref={addButton}
            variant="outline"
            size="sm"
            disabled={!providers.length}
            onClick={() => setAdding(true)}
          >
            <RiAddLine data-icon="inline-start" />
            Add connection
          </Button>
        </div>
        {connections.length ? (
          <Table className="table-fixed">
            <TableHeader>
              <TableRow>
                <TableHead className="pl-4">Name</TableHead>
                <TableHead className="hidden sm:table-cell">Provider</TableHead>
                <TableHead className="w-32">Status</TableHead>
                <TableHead className="w-12">
                  <span className="sr-only">Actions</span>
                </TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {connections.map((connection) => (
                <ConnectionRow
                  key={connection.id}
                  workspaceId={workspaceId}
                  connection={connection}
                  providers={providers}
                  removeReturnFocus={addButton}
                />
              ))}
            </TableBody>
          </Table>
        ) : (
          <Empty className="py-10">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <RiKey2Line />
              </EmptyMedia>
              <EmptyTitle>No connections yet</EmptyTitle>
              <EmptyDescription>
                Add a provider key, then select a connection and model for
                Hondo.
              </EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        You can add more than one connection to the same provider.
      </p>
      {adding && (
        <ConnectionDialog
          workspaceId={workspaceId}
          providers={providers}
          returnFocus={addButton}
          existingNames={connections.map((item) => item.name)}
          onClose={() => setAdding(false)}
        />
      )}
    </div>
  )
}
