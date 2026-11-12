import { useState } from "react"
import { Link, Navigate, useNavigate, useParams } from "react-router"
import { RiArrowLeftLine, RiRefreshLine } from "@remixicon/react"
import { toast } from "sonner"
import { ChannelPicker } from "@/components/channel-picker"
import { ErrorCard } from "@/components/error-card"
import { PageHeading } from "@/components/page-heading"
import { Button } from "@/components/ui/button"
import { FieldDescription, FieldLegend, FieldSet } from "@/components/ui/field"
import { useBuiltinConnectors, useMCPAction } from "@/lib/api/mcp"
import type { MCPConnection } from "@/lib/api/types"
import {
  AccountConnect,
  MissingBuiltinSetup,
  type AccountKey,
} from "./account-connect"
import { connectorsPath, toolPath, useMCPPage } from "./context"
import { errorMessage, keyErrorMessage } from "./helpers"
import { AddToolButton, ToolsTable } from "./tools-table"

function defaultTools(connection: MCPConnection) {
  const approved = connection.tools.filter((tool) => tool.enabled)
  return (
    approved.length
      ? approved
      : connection.tools.filter((tool) => tool.read_only)
  ).map((tool) => tool.name)
}

function defaultAsk(connection: MCPConnection) {
  return connection.tools
    .filter((tool) => tool.enabled && tool.ask)
    .map((tool) => tool.name)
}

function Account({ connection }: { connection: MCPConnection }) {
  const { workspace } = useMCPPage()
  const action = useMCPAction(workspace.id)
  const catalog = useBuiltinConnectors(
    workspace.id,
    connection.builtin !== null
  )
  const builtin = catalog.data?.connectors.find(
    (item) => item.id === connection.builtin
  )

  async function saveKey(key: AccountKey) {
    try {
      const result = await action.mutateAsync(
        "headers" in key
          ? { type: "key", id: connection.id, body: key }
          : { type: "builtin-key", id: connection.id, body: key }
      )
      if (result?.status === "unavailable")
        toast.error(
          "Key saved, but Hondo could not load the tools. Make sure the key has access, then refresh the tools."
        )
    } catch (cause) {
      toast.error(keyErrorMessage(key, cause))
    }
  }

  if (connection.builtin !== null && !builtin)
    return <MissingBuiltinSetup catalog={catalog} />
  return (
    <AccountConnect
      workspaceId={workspace.id}
      connection={connection}
      builtin={builtin}
      pending={action.isPending}
      onSaveKey={saveKey}
    />
  )
}

function Access({ connection }: { connection: MCPConnection }) {
  const { workspace } = useMCPPage()
  const navigate = useNavigate()
  const action = useMCPAction(workspace.id)
  const [tools, setTools] = useState(() => defaultTools(connection))
  const [ask, setAsk] = useState(() => defaultAsk(connection))
  const [allChannels, setAllChannels] = useState(connection.all_channels)
  const [channels, setChannels] = useState(connection.channels)
  const http = connection.kind === "http"
  const newTool = toolPath(workspace.id, connection.id)
  const missing =
    tools.length === 0
      ? "Select at least one tool."
      : !allChannels && channels.length === 0
        ? "Select at least one channel."
        : null
  const plural = (count: number, noun: string) =>
    `${count} ${noun}${count === 1 ? "" : "s"}`
  const summary =
    missing ??
    `${plural(tools.length, "tool")} in ${allChannels ? "all channels" : plural(channels.length, "channel")}`

  async function refresh() {
    try {
      await action.mutateAsync({ type: "refresh", id: connection.id })
    } catch (cause) {
      toast.error(errorMessage(cause))
    }
  }

  async function turnOn() {
    if (missing || action.isPending) return
    try {
      await action.mutateAsync({
        type: "save",
        id: connection.id,
        body: {
          name: connection.name,
          revision: connection.revision,
          enabled: true,
          all_channels: allChannels,
          channels: allChannels ? [] : channels,
          allowed_tools: tools,
          ask_tools: ask,
          review_tools: true,
        },
      })
      navigate(connectorsPath(workspace.id, connection.id), { replace: true })
      toast.success(`${connection.name} is turned on.`)
    } catch (cause) {
      toast.error(errorMessage(cause))
    }
  }

  return (
    <div className="flex w-full max-w-3xl min-w-0 flex-col gap-10">
      <FieldSet className="gap-5">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <FieldLegend>Tools</FieldLegend>
            <FieldDescription>
              Tools without Ask first run without approval.
            </FieldDescription>
          </div>
          {http && connection.tools.length > 0 && (
            <AddToolButton to={newTool} disabled={action.isPending} />
          )}
        </div>
        {http && connection.tools.length === 0 ? (
          <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-12">
            <p className="text-sm text-muted-foreground">
              Add a tool for each request that Hondo can make to this API.
            </p>
            <AddToolButton to={newTool} disabled={action.isPending} />
          </div>
        ) : connection.tools.length === 0 ? (
          <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-12">
            <p className="text-sm text-muted-foreground">
              Hondo could not load the tools from this server.
            </p>
            <Button
              type="button"
              variant="outline"
              disabled={action.isPending}
              onClick={() => void refresh()}
            >
              <RiRefreshLine data-icon="inline-start" />
              Refresh tools
            </Button>
          </div>
        ) : (
          <ToolsTable
            tools={connection.tools}
            selected={tools}
            ask={ask}
            pending={action.isPending}
            toolLink={
              http
                ? (name) => toolPath(workspace.id, connection.id, name)
                : undefined
            }
            onChange={(next, nextAsk) => {
              setTools(next)
              setAsk(nextAsk)
            }}
          />
        )}
      </FieldSet>
      <ChannelPicker
        workspaceId={workspace.id}
        description="Select where Hondo can use these tools. Direct messages are excluded."
        allChannels={allChannels}
        onAllChannelsChange={setAllChannels}
        channels={channels}
        savedChannels={connection.channels}
        onChannelsChange={setChannels}
        disabled={action.isPending}
      />
      <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-6">
        <p className="text-sm">{summary}</p>
        <Button
          type="button"
          onClick={() => void turnOn()}
          disabled={action.isPending || missing !== null}
        >
          {action.isPending ? "Turning on…" : "Turn on"}
        </Button>
      </div>
    </div>
  )
}

export default function MCPSetupPage() {
  const { connectionId } = useParams()
  const { connections, workspace } = useMCPPage()
  const connection = connections.find((item) => item.id === connectionId)
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
  if (connection.enabled)
    return <Navigate to={connectorsPath(workspace.id, connection.id)} replace />
  const needsSignIn =
    !connection.connected || connection.status === "sign_in_required"
  return (
    <>
      <Link
        to={connectorsPath(workspace.id)}
        className="flex w-fit items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
      >
        <RiArrowLeftLine className="size-4" aria-hidden="true" />
        Connectors
      </Link>
      <PageHeading
        title={`Set up ${connection.name}`}
        description={connection.url}
      />
      {needsSignIn ? (
        <Account key={connection.revision} connection={connection} />
      ) : (
        <Access key={connection.revision} connection={connection} />
      )}
    </>
  )
}
