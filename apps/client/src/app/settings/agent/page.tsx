import { useId, useRef, useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"

import { ErrorCard } from "@/components/error-card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Combobox,
  ComboboxContent,
  ComboboxEmpty,
  ComboboxInput,
  ComboboxItem,
  ComboboxList,
} from "@/components/ui/combobox"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { ApiError } from "@/lib/api/client"
import {
  useAgentModel,
  useConnectionModels,
  useProviders,
  useSaveAgentModel,
} from "@/lib/api/queries"
import type {
  AgentModel,
  ProviderConnection,
  ProviderModel,
  Workspace,
} from "@/lib/api/types"

function modelError(error: unknown): string {
  if (error instanceof ApiError) {
    if (
      error.detail === "connection_required" ||
      error.detail === "connection_not_found"
    )
      return "Add or check a connection before you save a model."
    if (error.detail === "model_unavailable")
      return "This model is no longer available. Refresh the list and select another model."
    if (error.detail === "connections_unavailable")
      return "Hondo can't read this connection's saved key. Edit the connection and enter the key again."
    if (error.status === 401) return "Your session has expired. Sign in again."
  }
  return "The model could not be saved. Try again."
}

function ModelPicker({
  workspaceId,
  connection,
  value,
  onChange,
  disabled,
}: {
  workspaceId: string
  connection: ProviderConnection
  value: string | null
  onChange: (value: string | null) => void
  disabled: boolean
}) {
  const id = useId()
  const query = useConnectionModels(workspaceId, connection.id)
  if (query.isPending) return <Skeleton className="h-9 w-full" />
  if (query.isError)
    return (
      <Alert>
        <AlertDescription className="flex flex-col items-start gap-2">
          The model list could not be loaded.
          <Button
            type="button"
            variant="outline"
            disabled={query.isFetching}
            onClick={() => void query.refetch()}
          >
            Try again
          </Button>
        </AlertDescription>
      </Alert>
    )
  const selected = query.data.find((model) => model.id === value) ?? null
  if (query.data.length === 0)
    return (
      <Alert>
        <AlertDescription>
          No supported models are available from {connection.name} for this API
          key.
        </AlertDescription>
      </Alert>
    )
  return (
    <Field>
      <FieldLabel htmlFor={`${id}-model`}>Model</FieldLabel>
      <Combobox<ProviderModel>
        items={query.data}
        value={selected}
        onValueChange={(model: ProviderModel | null) =>
          onChange(model?.id ?? null)
        }
        itemToStringLabel={(model) => model.name}
        isItemEqualToValue={(item, current) => item.id === current.id}
        filter={(model, search) =>
          `${model.name} ${model.id}`
            .toLowerCase()
            .includes(search.toLowerCase())
        }
        disabled={disabled}
      >
        <ComboboxInput
          id={`${id}-model`}
          autoFocus
          disabled={disabled}
          placeholder="Search models…"
          aria-describedby={`${id}-help`}
        />
        <ComboboxContent>
          <ComboboxEmpty>No matching models.</ComboboxEmpty>
          <ComboboxList>
            {(model: ProviderModel) => (
              <ComboboxItem key={model.id} value={model} className="pr-8">
                <div className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="truncate">{model.name}</span>
                  <span className="truncate text-xs text-muted-foreground">
                    {model.id}
                  </span>
                </div>
                {model.reads_images && (
                  <Badge variant="outline" className="shrink-0">
                    Reads images
                  </Badge>
                )}
              </ComboboxItem>
            )}
          </ComboboxList>
        </ComboboxContent>
      </Combobox>
      <FieldDescription id={`${id}-help`}>
        Models with tool support for {connection.name}. Access and cost depend
        on your account.
      </FieldDescription>
    </Field>
  )
}

function ModelForm({
  workspaceId,
  saved,
  connections,
}: {
  workspaceId: string
  saved: AgentModel
  connections: (ProviderConnection & { providerName: string })[]
}) {
  const id = useId()
  const [editing, setEditing] = useState(false)
  const editButton = useRef<HTMLButtonElement>(null)
  const [modelName, setModelName] = useState(saved.model_name)
  const [connectionId, setConnectionId] = useState<string | undefined>(
    connections.find((item) => item.id === saved.connection_id)?.id ??
      connections[0]?.id
  )
  const connection = connections.find((item) => item.id === connectionId)
  const [error, setError] = useState<string | null>(null)
  const action = useSaveAgentModel(workspaceId)

  function closeEditor() {
    setEditing(false)
    setError(null)
    requestAnimationFrame(() => editButton.current?.focus())
  }

  async function save(remove: boolean) {
    if (!remove && (!modelName || !connection)) return
    setError(null)
    try {
      await action.mutateAsync(
        remove
          ? null
          : { connection_id: connection!.id, model_name: modelName! }
      )
      toast.success(
        remove ? "Model removed." : "Model saved. It applies to the next run."
      )
      closeEditor()
    } catch (cause) {
      setError(modelError(cause))
    }
  }

  if (!editing) {
    return (
      <Button
        ref={editButton}
        variant="outline"
        className="w-fit"
        onClick={() => {
          const initial =
            connections.find((item) => item.id === saved.connection_id) ??
            connections[0]
          setConnectionId(initial?.id)
          setModelName(
            initial?.id === saved.connection_id ? saved.model_name : null
          )
          setError(null)
          setEditing(true)
        }}
      >
        {saved.source === "unconfigured" ? "Select model" : "Change model"}
      </Button>
    )
  }

  return (
    <form
      aria-label="Change model"
      className="border-t pt-5"
      onSubmit={(event) => {
        event.preventDefault()
        void save(false)
      }}
    >
      <FieldGroup>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {connections.length > 0 ? (
          <>
            <Field>
              <FieldLabel htmlFor={`${id}-connection`}>Connection</FieldLabel>
              <Select
                items={connections.map((item) => ({
                  value: item.id,
                  label: `${item.name} · ${item.providerName}`,
                }))}
                value={connection?.id ?? null}
                onValueChange={(value) => {
                  if (value === connectionId) return
                  setConnectionId(value ?? undefined)
                  setModelName(null)
                  setError(null)
                }}
                disabled={action.isPending}
              >
                <SelectTrigger id={`${id}-connection`} className="w-full">
                  <SelectValue placeholder="Select connection" />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    {connections.map((item) => (
                      <SelectItem key={item.id} value={item.id}>
                        {item.name} · {item.providerName}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                </SelectContent>
              </Select>
            </Field>
            {connection && (
              <ModelPicker
                key={connection.id}
                workspaceId={workspaceId}
                connection={connection}
                value={modelName}
                onChange={setModelName}
                disabled={action.isPending}
              />
            )}
          </>
        ) : (
          <Alert>
            <AlertDescription>
              <Link
                to="/settings#connections"
                className="underline underline-offset-4"
              >
                Add a connection
              </Link>{" "}
              in Connections to select a model.
            </AlertDescription>
          </Alert>
        )}
        <Field>
          <div className="flex flex-wrap gap-2">
            {connection && (
              <Button
                type="submit"
                disabled={
                  action.isPending ||
                  !modelName ||
                  (connection.id === saved.connection_id &&
                    modelName === saved.model_name)
                }
              >
                {action.isPending ? "Saving…" : "Save model"}
              </Button>
            )}
            <Button
              type="button"
              variant="outline"
              disabled={action.isPending}
              onClick={closeEditor}
            >
              Cancel
            </Button>
            {saved.source === "connection" && (
              <Button
                type="button"
                variant="outline"
                disabled={action.isPending}
                onClick={() => void save(true)}
              >
                Remove model
              </Button>
            )}
          </div>
          <FieldDescription>
            Changes apply to new runs. An active run keeps its current model.
          </FieldDescription>
        </Field>
      </FieldGroup>
    </form>
  )
}

export function AgentSettings({ workspace }: { workspace: Workspace }) {
  const selection = useAgentModel(workspace.id)
  const connections = useProviders(workspace.id)
  if (selection.isPending || connections.isPending)
    return <Skeleton className="h-64 w-full" />
  if (selection.isError || connections.isError)
    return (
      <ErrorCard
        title="Could not load agent settings"
        onRetry={() => {
          void selection.refetch()
          void connections.refetch()
        }}
        retrying={selection.isFetching || connections.isFetching}
      />
    )
  const saved = selection.data
  const savedConnection = connections.data.connections.find(
    (item) => item.id === saved.connection_id
  )
  const availableConnections = connections.data.connections
    .filter((item) => item.check_status === "valid")
    .map((item) => ({
      ...item,
      providerName:
        connections.data.providers.find(
          (provider) => provider.id === item.provider
        )?.name ?? item.provider,
    }))
  return (
    <Card>
      <CardHeader>
        <CardTitle>Hondo’s model</CardTitle>
        <CardDescription>
          {saved.source === "unconfigured"
            ? "Select a connection and model so Hondo can answer in Slack."
            : `Uses ${savedConnection?.name ?? "a saved connection"}.`}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6">
        <div className="flex flex-col gap-1 rounded-lg border bg-muted/40 p-5">
          <p className="text-xs text-muted-foreground">Current selection</p>
          <p className="text-base font-medium break-all">
            {saved.model_name ?? "No model selected"}
          </p>
        </div>
        {saved.source !== "unconfigured" && !saved.ready && (
          <Alert>
            <AlertDescription>
              This selection is not ready. Check the saved connection, or select
              another connection and model.
            </AlertDescription>
          </Alert>
        )}
        <ModelForm
          workspaceId={workspace.id}
          saved={saved}
          connections={availableConnections}
        />
      </CardContent>
    </Card>
  )
}
