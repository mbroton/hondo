import { useId, useState } from "react"
import {
  Link,
  Navigate,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router"
import {
  RiArrowLeftLine,
  RiDeleteBinLine,
  RiRefreshLine,
} from "@remixicon/react"
import { toast } from "sonner"
import { ChannelPicker } from "@/components/channel-picker"
import { ErrorCard } from "@/components/error-card"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  needsSlackReinstall,
  useBuiltinConnectors,
  useMCPAction,
  useMCPChannels,
} from "@/lib/api/mcp"
import type { BuiltinConnector, MCPConnection } from "@/lib/api/types"
import { ConnectorName } from "./connector-name"
import { KeyConnect } from "./key-connect"
import { AddToolButton, ToolsTable } from "./tools-table"
import {
  AccountConnect,
  MissingBuiltinSetup,
  type AccountKey,
} from "./account-connect"
import { connectorsPath, setupPath, toolPath, useMCPPage } from "./context"
import { errorMessage, keyErrorMessage } from "./helpers"
import { ConnectionStatus } from "./status"

function askedTools(connection: MCPConnection) {
  return connection.tools
    .filter((tool) => tool.enabled && tool.ask)
    .map((tool) => tool.name)
}

function ConnectionDetails({ connection }: { connection: MCPConnection }) {
  const { workspace } = useMCPPage()
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const requestedTab =
    params.get("tab") ??
    (params.get("mcp") === "connected" ? "tools" : "overview")
  const tab = ["overview", "tools", "access"].includes(requestedTab)
    ? requestedTab
    : "overview"
  const id = useId()
  const action = useMCPAction(workspace.id)
  const [saved, setSaved] = useState(connection)
  const latest = connection.revision >= saved.revision ? connection : saved
  const needsSignIn = !latest.connected || latest.status === "sign_in_required"
  const [name, setName] = useState(saved.name)
  const [tools, setTools] = useState(
    saved.tools.filter((tool) => tool.enabled).map((tool) => tool.name)
  )
  const [ask, setAsk] = useState(askedTools(saved))
  const [allChannels, setAllChannels] = useState(saved.all_channels)
  const [channels, setChannels] = useState(saved.channels)
  const [enabled, setEnabled] = useState(saved.enabled)
  const [reconnecting, setReconnecting] = useState(false)
  const [confirm, setConfirm] = useState<"delete" | "disconnect" | null>(null)
  const connecting = confirm !== "disconnect" && (reconnecting || needsSignIn)
  const http = saved.kind === "http"
  const channelQuery = useMCPChannels(workspace.id)
  const catalog = useBuiltinConnectors(workspace.id, saved.builtin !== null)
  const builtin = catalog.data?.connectors.find(
    (item) => item.id === saved.builtin
  )
  const setupMissing = saved.builtin !== null && !builtin
  const workspaceReconnectRequired = needsSlackReinstall(channelQuery.error)
  const reviewing = saved.review_required && tab === "tools"
  const missing = !enabled
    ? null
    : tools.length === 0
      ? "Select at least one tool to enable the connector."
      : !allChannels && channels.length === 0
        ? "Select at least one channel to enable the connector."
        : null
  const changed =
    name !== saved.name ||
    enabled !== saved.enabled ||
    allChannels !== saved.all_channels ||
    channels.toSorted().join() !== saved.channels.toSorted().join() ||
    tools.toSorted().join() !==
      saved.tools
        .filter((tool) => tool.enabled)
        .map((tool) => tool.name)
        .toSorted()
        .join() ||
    ask.toSorted().join() !== askedTools(saved).toSorted().join()

  function selectTab(value: string) {
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        next.set("tab", value)
        return next
      },
      { replace: true }
    )
  }

  function clearNotice() {
    setParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        next.delete("mcp")
        next.set("tab", tab)
        return next
      },
      { replace: true }
    )
  }

  function reset(next: MCPConnection) {
    setSaved(next)
    setName(next.name)
    setTools(next.tools.filter((tool) => tool.enabled).map((tool) => tool.name))
    setAsk(askedTools(next))
    setAllChannels(next.all_channels)
    setChannels(next.channels)
    setEnabled(next.enabled)
    setReconnecting(false)
    setConfirm(null)
    action.reset()
  }

  async function run(type: "refresh" | "disconnect" | "delete") {
    try {
      const result = await action.mutateAsync({ type, id: saved.id })
      if (type === "refresh") {
        if (result) reset(result)
        clearNotice()
        toast.success("Tools refreshed. Review the current list.")
      } else if (type === "disconnect") {
        setConfirm(null)
        navigate(connectorsPath(workspace.id), { replace: true })
        toast.success("Account disconnected.")
      } else {
        navigate(connectorsPath(workspace.id), { replace: true })
        toast.success("Connector removed.")
      }
    } catch (cause) {
      toast.error(errorMessage(cause))
    }
  }

  async function saveKey(key: AccountKey) {
    try {
      const result = await action.mutateAsync(
        "headers" in key
          ? { type: "key", id: saved.id, body: key }
          : { type: "builtin-key", id: saved.id, body: key }
      )
      if (!result) return
      if (!result.enabled) {
        navigate(setupPath(workspace.id, result.id), { replace: true })
        return
      }
      reset(result)
      if (result.status === "ready") {
        selectTab("tools")
        toast.success("Key saved. Select the tools that Hondo can use.")
      } else if (result.status === "unavailable") {
        toast.error(
          "Key saved, but Hondo could not load the tools. Make sure the key has access, then refresh the tools."
        )
      }
    } catch (cause) {
      toast.error(keyErrorMessage(key, cause))
    }
  }

  async function save() {
    if ((!changed && !reviewing) || !name.trim() || missing || action.isPending)
      return
    try {
      const result = await action.mutateAsync({
        type: "save",
        id: saved.id,
        body: {
          name,
          revision: saved.revision,
          enabled,
          all_channels: allChannels,
          channels: allChannels ? [] : channels,
          allowed_tools: tools,
          ask_tools: ask,
          review_tools: reviewing,
        },
      })
      if (result) reset(result)
      clearNotice()
      toast.success("Connector saved.")
    } catch (cause) {
      toast.error(errorMessage(cause))
    }
  }

  return (
    <>
      <Link
        to={connectorsPath(workspace.id)}
        className="flex w-fit items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
      >
        <RiArrowLeftLine className="size-4" aria-hidden="true" />
        Connectors
      </Link>
      <title>{`${name} · Hondo`}</title>
      <div className="flex flex-col items-start gap-4 sm:flex-row sm:justify-between">
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <ConnectorName
            key={saved.revision}
            name={name}
            disabled={action.isPending}
            onChange={setName}
          />
          <p className="text-sm wrap-anywhere text-muted-foreground">
            {saved.url}
          </p>
          <ConnectionStatus connection={latest} />
        </div>
        <div className="flex shrink-0 items-center gap-4">
          <Field orientation="horizontal" className="w-auto">
            <Switch
              id={`${id}-enabled`}
              aria-label="Enable connector"
              checked={enabled}
              onCheckedChange={setEnabled}
              disabled={action.isPending || (!enabled && needsSignIn)}
            />
            <FieldLabel htmlFor={`${id}-enabled`}>
              <span className="sr-only">Enable connector</span>
              <span aria-hidden="true">{enabled ? "Enabled" : "Disabled"}</span>
            </FieldLabel>
          </Field>
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label="Remove connector"
            onClick={() => setConfirm("delete")}
            disabled={action.isPending}
          >
            <RiDeleteBinLine data-icon="inline-start" />
            Remove
          </Button>
        </div>
      </div>
      {confirm === "delete" && (
        <Alert variant="destructive">
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>Remove this connector and its saved access?</span>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setConfirm(null)}
                disabled={action.isPending}
              >
                Cancel
              </Button>
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={() => void run("delete")}
                disabled={action.isPending}
              >
                Confirm removal
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}
      {workspaceReconnectRequired && (
        <Alert>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>
              Connectors need more Slack permissions. Reinstall Hondo in Slack
              from workspace settings to restore access.
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
      {action.isError && (
        <Alert variant="destructive">
          <AlertDescription>{errorMessage(action.error)}</AlertDescription>
        </Alert>
      )}
      {latest.revision > saved.revision && (
        <Alert>
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>This connector changed. Your edits are still here.</span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => reset(latest)}
              disabled={action.isPending}
            >
              {changed ? "Discard changes" : "Load latest settings"}
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <div className="flex min-w-0 flex-col gap-6">
        <Tabs
          value={tab}
          onValueChange={(value) => selectTab(String(value))}
          className="gap-7"
        >
          <div className="border-b pb-1">
            <TabsList variant="line" aria-label="Connector sections">
              <TabsTrigger value="overview" className="px-4">
                Overview
              </TabsTrigger>
              <TabsTrigger value="tools" className="px-4">
                Tools{" "}
                <span className="text-muted-foreground">{tools.length}</span>
              </TabsTrigger>
              <TabsTrigger value="access" className="px-4">
                Access
              </TabsTrigger>
            </TabsList>
          </div>
          <TabsContent value="overview" className="flex flex-col gap-10">
            {saved.builtin !== null && (
              <BuiltinSummary
                id={saved.builtin}
                builtin={builtin}
                loading={catalog.isPending}
              />
            )}
            <section
              aria-labelledby={`${id}-account`}
              className="flex max-w-3xl flex-col gap-5"
            >
              <div>
                <h2 id={`${id}-account`} className="text-sm font-medium">
                  Account
                </h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  People in the allowed channels use this same account,
                  including when they steer Hondo.
                </p>
              </div>
              {latest.connected && (
                <div className="flex flex-wrap items-center justify-between gap-4 rounded-lg border p-4">
                  <div className="flex flex-col gap-1">
                    <p className="text-sm font-medium">
                      {http
                        ? latest.authorized_by_name
                          ? `Added by ${latest.authorized_by_name}`
                          : "Added"
                        : latest.auth === "key"
                          ? latest.authorized_by_name
                            ? `Key added by ${latest.authorized_by_name}`
                            : "Key added"
                          : latest.authorized_by_name
                            ? `Connected by ${latest.authorized_by_name}`
                            : "Account connected"}
                    </p>
                    <p className="text-sm text-muted-foreground">
                      {http && latest.header_names.length === 0
                        ? "No headers"
                        : latest.auth === "key" && latest.builtin === null
                          ? `Headers: ${latest.header_names.join(", ")}`
                          : "Shared with this workspace"}
                    </p>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setReconnecting(true)
                        setConfirm(null)
                      }}
                      disabled={action.isPending || changed || connecting}
                    >
                      {http
                        ? "Replace headers"
                        : latest.auth === "key"
                          ? "Replace key"
                          : "Reconnect"}
                    </Button>
                    {!http && (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => {
                          setReconnecting(false)
                          setConfirm("disconnect")
                        }}
                        disabled={action.isPending}
                      >
                        Disconnect
                      </Button>
                    )}
                  </div>
                </div>
              )}
              {confirm === "disconnect" && (
                <Alert>
                  <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
                    <span>
                      Disconnect this account and stop further tool calls?
                    </span>
                    <div className="flex gap-2">
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => setConfirm(null)}
                        disabled={action.isPending}
                      >
                        Cancel
                      </Button>
                      <Button
                        type="button"
                        variant="destructive"
                        size="sm"
                        onClick={() => void run("disconnect")}
                        disabled={action.isPending}
                      >
                        Confirm disconnect
                      </Button>
                    </div>
                  </AlertDescription>
                </Alert>
              )}
              {connecting && setupMissing && (
                <MissingBuiltinSetup catalog={catalog} />
              )}
              {connecting && !setupMissing && (
                <fieldset
                  disabled={action.isPending || changed}
                  className="min-w-0"
                >
                  {http ? (
                    <div className="max-w-2xl">
                      <KeyConnect
                        pending={action.isPending}
                        minHeaders={0}
                        onSave={(headers) => saveKey({ headers })}
                      />
                    </div>
                  ) : (
                    <AccountConnect
                      key={saved.revision}
                      workspaceId={workspace.id}
                      connection={latest}
                      builtin={builtin}
                      pending={action.isPending}
                      onSaveKey={saveKey}
                    />
                  )}
                  {!needsSignIn && (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="mt-3"
                      onClick={() => setReconnecting(false)}
                    >
                      Cancel
                    </Button>
                  )}
                </fieldset>
              )}
              {changed && (
                <p className="text-sm text-muted-foreground">
                  Save or discard your changes before reconnecting.
                </p>
              )}
            </section>
          </TabsContent>
          <TabsContent value="tools">
            <FieldSet className="gap-5">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div>
                  <FieldLegend>Allowed tools</FieldLegend>
                  <FieldDescription>
                    {tools.length} of {saved.tools.length} selected. Tools
                    without Ask first run without approval.
                  </FieldDescription>
                </div>
                {http ? (
                  <AddToolButton
                    to={toolPath(workspace.id, saved.id)}
                    disabled={action.isPending || changed}
                  />
                ) : (
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    disabled={!latest.connected || action.isPending || changed}
                    onClick={() => void run("refresh")}
                  >
                    <RiRefreshLine data-icon="inline-start" />
                    Refresh tools
                  </Button>
                )}
              </div>
              {saved.review_required && (
                <Alert>
                  <AlertDescription>
                    The tool list changed. Review it and save your selection.
                  </AlertDescription>
                </Alert>
              )}
              {changed && (
                <p className="text-sm text-muted-foreground">
                  {http
                    ? "Save or discard your changes before adding tools."
                    : "Save or discard your changes before refreshing tools."}
                </p>
              )}
              {saved.tools.length === 0 ? (
                <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-12">
                  <p className="text-sm text-muted-foreground">
                    {http
                      ? "Add a tool for each request that Hondo can make to this API."
                      : latest.connected
                        ? "Refresh to load this server’s tools."
                        : "Connect an account to load its tools."}
                  </p>
                  {!latest.connected && !http && (
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => selectTab("overview")}
                    >
                      Go to account
                    </Button>
                  )}
                </div>
              ) : (
                <ToolsTable
                  tools={saved.tools}
                  selected={tools}
                  ask={ask}
                  pending={action.isPending}
                  onChange={(next, nextAsk) => {
                    setTools(next)
                    setAsk(nextAsk)
                  }}
                  toolLink={
                    http && !changed
                      ? (name) => toolPath(workspace.id, saved.id, name)
                      : undefined
                  }
                />
              )}
            </FieldSet>
          </TabsContent>
          <TabsContent value="access">
            <FieldGroup className="max-w-3xl gap-8">
              <ChannelPicker
                workspaceId={workspace.id}
                description="Select where Hondo can use these tools. Direct messages are excluded."
                allChannels={allChannels}
                onAllChannelsChange={setAllChannels}
                channels={channels}
                savedChannels={saved.channels}
                onChannelsChange={setChannels}
                disabled={action.isPending}
              />
            </FieldGroup>
          </TabsContent>
        </Tabs>
        {(changed || reviewing) && (
          <div className="sticky bottom-3 z-10 flex flex-wrap items-center justify-between gap-3 rounded-lg border bg-background p-3 shadow-sm">
            <p className="text-sm">
              {missing ??
                (reviewing
                  ? "Review the tool selection, then save."
                  : "Unsaved changes")}
            </p>
            <div className="flex gap-2">
              {changed && (
                <Button
                  type="button"
                  variant="outline"
                  onClick={() => reset(latest)}
                  disabled={action.isPending}
                >
                  Discard changes
                </Button>
              )}
              <Button
                type="button"
                onClick={() => void save()}
                disabled={action.isPending || !name.trim() || missing !== null}
              >
                {action.isPending
                  ? "Saving…"
                  : reviewing
                    ? changed
                      ? "Save changes and tool selection"
                      : "Save tool selection"
                    : "Save changes"}
              </Button>
            </div>
          </div>
        )}
      </div>
    </>
  )
}

function BuiltinSummary({
  id,
  builtin,
  loading,
}: {
  id: string
  builtin: BuiltinConnector | undefined
  loading: boolean
}) {
  const headingId = useId()
  return (
    <section
      aria-labelledby={headingId}
      className="flex max-w-3xl flex-col gap-5"
    >
      <div>
        <h2 id={headingId} className="text-sm font-medium">
          Built-in connector
        </h2>
        {loading ? (
          <Skeleton className="mt-2 h-4 w-64" />
        ) : (
          <p className="mt-1 text-sm text-muted-foreground">
            {builtin ? `${builtin.title}: ${builtin.description}` : id}
          </p>
        )}
      </div>
      {builtin && builtin.skills.length > 0 && (
        <div className="flex flex-col gap-2">
          <p id={`${headingId}-skills`} className="text-sm font-medium">
            Skills
          </p>
          <p className="text-sm text-muted-foreground">
            Hondo can use these skills in the channels where this connector is
            enabled.
          </p>
          <ul
            aria-labelledby={`${headingId}-skills`}
            className="flex flex-col divide-y rounded-lg border"
          >
            {builtin.skills.map((skill) => (
              <li key={skill.name} className="flex flex-col gap-1 p-4">
                <span className="text-sm font-medium">{skill.name}</span>
                <span className="text-sm text-muted-foreground">
                  {skill.description}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  )
}

export default function MCPDetailsPage() {
  const { connectionId } = useParams()
  const { connections, workspace } = useMCPPage()
  const [params] = useSearchParams()
  const connection = connections.find((item) => item.id === connectionId)
  if (connection && params.has("mcp") && !connection.enabled) {
    const next = new URLSearchParams(params)
    if (next.get("mcp") === "connected") next.delete("mcp")
    next.delete("tab")
    const search = next.size ? `?${next}` : ""
    return (
      <Navigate
        to={`${setupPath(workspace.id, connection.id)}${search}`}
        replace
      />
    )
  }
  if (!connection)
    return (
      <>
        <Link
          to={connectorsPath(workspace.id)}
          className="text-sm underline underline-offset-4"
        >
          Back to connectors
        </Link>
        <ErrorCard title="Connector not found" />
      </>
    )
  return <ConnectionDetails key={connection.id} connection={connection} />
}
