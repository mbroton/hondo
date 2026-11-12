import { useId, useState } from "react"
import { Link, useNavigate, useParams } from "react-router"
import { RiArrowLeftLine, RiDeleteBinLine } from "@remixicon/react"
import { toast } from "sonner"
import { ErrorCard } from "@/components/error-card"
import { PageHeading } from "@/components/page-heading"
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
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { useHTTPToolTest, useMCPAction } from "@/lib/api/mcp"
import type { HTTPTool, MCPConnection } from "@/lib/api/types"
import { connectorsPath, setupPath, toolPath, useMCPPage } from "./context"
import { errorMessage, toolErrorMessage } from "./helpers"

type Tool = MCPConnection["tools"][number]

const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
const PLACEHOLDER = /\{([A-Za-z_][A-Za-z0-9_]{0,63})\}/g

function placeholders(...texts: string[]) {
  return [
    ...new Set(
      texts.flatMap((text) =>
        [...text.matchAll(PLACEHOLDER)].map((match) => match[1])
      )
    ),
  ]
}

function connectorPath(workspaceId: string, connection: MCPConnection) {
  return connection.enabled
    ? `${connectorsPath(workspaceId, connection.id)}?tab=tools`
    : setupPath(workspaceId, connection.id)
}

function draftOf(connection: MCPConnection, tool: Tool | undefined): HTTPTool {
  const request = tool?.request
  return {
    name: tool?.name ?? "",
    description: tool?.description ?? "",
    method: request?.method ?? "GET",
    url: request?.url ?? `${connection.url.replace(/\/+$/, "")}/`,
    body: request?.body ?? "",
    params: request?.params ?? [],
  }
}

function ToolEditor({
  connection,
  tool,
}: {
  connection: MCPConnection
  tool: Tool | undefined
}) {
  const { workspace } = useMCPPage()
  const navigate = useNavigate()
  const id = useId()
  const action = useMCPAction(workspace.id)
  const saved = draftOf(connection, tool)
  const [draft, setDraft] = useState(saved)
  const [descriptions, setDescriptions] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      saved.params.map((param) => [param.name, param.description])
    )
  )
  const [confirmRemove, setConfirmRemove] = useState(false)
  const names = placeholders(draft.url, draft.body ?? "")
  const params = names.map((name) => ({
    name,
    description: (descriptions[name] ?? "").trim(),
  }))
  const body: HTTPTool = {
    ...draft,
    body: draft.body?.trim() ? draft.body : null,
    params,
  }
  const changed =
    JSON.stringify(body) !==
    JSON.stringify({ ...saved, body: saved.body || null })
  const complete =
    draft.name.trim() &&
    draft.description.trim() &&
    draft.url.trim() &&
    params.every((param) => param.description)
  const methods = METHODS.includes(draft.method)
    ? METHODS
    : [...METHODS, draft.method]

  function update(change: Partial<HTTPTool>) {
    setDraft({ ...draft, ...change })
  }

  async function save() {
    if (!complete || action.isPending) return
    try {
      const result = await action.mutateAsync(
        tool
          ? { type: "save-tool", id: connection.id, tool: tool.name, body }
          : { type: "add-tool", id: connection.id, body }
      )
      toast.success("Tool saved.")
      if (result)
        navigate(toolPath(workspace.id, connection.id, body.name), {
          replace: true,
        })
    } catch {
      return
    }
  }

  async function remove() {
    if (!tool) return
    try {
      await action.mutateAsync({
        type: "delete-tool",
        id: connection.id,
        tool: tool.name,
      })
      toast.success("Tool removed.")
      navigate(connectorPath(workspace.id, connection), { replace: true })
    } catch (cause) {
      toast.error(errorMessage(cause))
    }
  }

  return (
    <>
      <Link
        to={connectorPath(workspace.id, connection)}
        className="flex w-fit items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
      >
        <RiArrowLeftLine className="size-4" aria-hidden="true" />
        {connection.name}
      </Link>
      <PageHeading
        title={tool ? tool.name : "Add tool"}
        description={connection.url}
      >
        {tool && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => setConfirmRemove(true)}
            disabled={action.isPending}
          >
            <RiDeleteBinLine data-icon="inline-start" />
            Remove tool
          </Button>
        )}
      </PageHeading>
      {confirmRemove && (
        <Alert variant="destructive" className="max-w-3xl">
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>Remove this tool from {connection.name}?</span>
            <div className="flex gap-2">
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setConfirmRemove(false)}
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
                Confirm removal
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      )}
      {tool && !tool.enabled && (
        <Alert className="max-w-3xl">
          <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
            <span>
              Hondo can use this tool only after an admin allows it on the
              Tools tab. A changed tool must be allowed again.
            </span>
            <Button
              variant="outline"
              size="sm"
              nativeButton={false}
              render={<Link to={connectorPath(workspace.id, connection)} />}
            >
              Go to tools
            </Button>
          </AlertDescription>
        </Alert>
      )}
      <Card className="max-w-3xl">
        <CardHeader>
          <CardTitle>Request</CardTitle>
          <CardDescription>
            Mark a value that Hondo fills with a name in braces, for example{" "}
            <code>{"{service}"}</code>. Hondo cannot change anything else.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form
            className="flex flex-col gap-6"
            onSubmit={(event) => {
              event.preventDefault()
              void save()
            }}
          >
            {action.isError && (
              <Alert variant="destructive">
                <AlertDescription>
                  {toolErrorMessage(action.error)}
                </AlertDescription>
              </Alert>
            )}
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
                <Input
                  id={`${id}-name`}
                  required
                  maxLength={64}
                  pattern="[A-Za-z0-9_.\-]+"
                  placeholder="search_logs"
                  value={draft.name}
                  onChange={(event) => update({ name: event.target.value })}
                  disabled={action.isPending}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor={`${id}-description`}>
                  Description
                </FieldLabel>
                <Textarea
                  id={`${id}-description`}
                  required
                  maxLength={2000}
                  placeholder="Search the logs of one service for a text."
                  value={draft.description}
                  onChange={(event) =>
                    update({ description: event.target.value })
                  }
                  disabled={action.isPending}
                />
                <FieldDescription>
                  Hondo reads this to know when to use the tool.
                </FieldDescription>
              </Field>
              <FieldSet>
                <FieldLegend variant="label">Method</FieldLegend>
                <ToggleGroup
                  variant="outline"
                  spacing={0}
                  aria-label="Method"
                  disabled={action.isPending}
                  value={[draft.method]}
                  onValueChange={([next]) => {
                    if (next) update({ method: next })
                  }}
                >
                  {methods.map((method) => (
                    <ToggleGroupItem key={method} value={method}>
                      {method}
                    </ToggleGroupItem>
                  ))}
                </ToggleGroup>
              </FieldSet>
              <Field>
                <FieldLabel htmlFor={`${id}-url`}>URL</FieldLabel>
                <Input
                  id={`${id}-url`}
                  required
                  maxLength={2048}
                  className="font-mono"
                  placeholder={`${connection.url.replace(/\/+$/, "")}/logs?service={service}`}
                  value={draft.url}
                  onChange={(event) => update({ url: event.target.value })}
                  disabled={action.isPending}
                />
              </Field>
              <Field>
                <FieldLabel htmlFor={`${id}-body`}>Body</FieldLabel>
                <Textarea
                  id={`${id}-body`}
                  maxLength={20000}
                  className="min-h-24 font-mono"
                  placeholder={'{"query": "{text}"}'}
                  value={draft.body ?? ""}
                  onChange={(event) => update({ body: event.target.value })}
                  disabled={action.isPending}
                />
                <FieldDescription>
                  Optional. JSON, sent as application/json. Put each
                  parameter inside quotes.
                </FieldDescription>
              </Field>
              {params.length > 0 && (
                <FieldSet className="gap-3">
                  <FieldLegend variant="label">Parameters</FieldLegend>
                  {params.map((param) => (
                    <Field key={param.name}>
                      <FieldLabel htmlFor={`${id}-param-${param.name}`}>
                        <code>{param.name}</code>
                      </FieldLabel>
                      <Input
                        id={`${id}-param-${param.name}`}
                        required
                        maxLength={500}
                        value={descriptions[param.name] ?? ""}
                        onChange={(event) =>
                          setDescriptions({
                            ...descriptions,
                            [param.name]: event.target.value,
                          })
                        }
                        disabled={action.isPending}
                      />
                    </Field>
                  ))}
                </FieldSet>
              )}
            </FieldGroup>
            <div className="flex justify-end">
              <Button
                type="submit"
                disabled={action.isPending || !complete || (!!tool && !changed)}
              >
                {action.isPending ? "Saving…" : "Save tool"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
      {tool?.request && (
        <TestCard
          key={JSON.stringify(tool.request)}
          connection={connection}
          tool={tool}
          changed={changed}
        />
      )}
    </>
  )
}

function TestCard({
  connection,
  tool,
  changed,
}: {
  connection: MCPConnection
  tool: Tool
  changed: boolean
}) {
  const { workspace } = useMCPPage()
  const id = useId()
  const test = useHTTPToolTest(workspace.id, connection.id)
  const params = tool.request?.params ?? []
  const [values, setValues] = useState<Record<string, string>>({})
  const complete = params.every((param) => (values[param.name] ?? "") !== "")
  return (
    <Card className="max-w-3xl">
      <CardHeader>
        <CardTitle>Test</CardTitle>
        <CardDescription>
          Send the saved request once with sample values. It runs now, without
          approval.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-6"
          onSubmit={(event) => {
            event.preventDefault()
            if (complete && !changed)
              test.mutate({ tool: tool.name, argumentValues: values })
          }}
        >
          {params.length > 0 && (
            <FieldGroup>
              {params.map((param) => (
                <Field key={param.name}>
                  <FieldLabel htmlFor={`${id}-${param.name}`}>
                    <code>{param.name}</code>
                  </FieldLabel>
                  <Input
                    id={`${id}-${param.name}`}
                    value={values[param.name] ?? ""}
                    onChange={(event) =>
                      setValues({ ...values, [param.name]: event.target.value })
                    }
                    disabled={test.isPending}
                  />
                  <FieldDescription>{param.description}</FieldDescription>
                </Field>
              ))}
            </FieldGroup>
          )}
          <div className="flex flex-wrap items-center justify-end gap-3">
            {changed && (
              <p className="text-sm text-muted-foreground">
                Save your changes to test them.
              </p>
            )}
            <Button
              type="submit"
              variant="outline"
              disabled={test.isPending || !complete || changed}
            >
              {test.isPending ? "Sending…" : "Send test request"}
            </Button>
          </div>
          {test.isError && (
            <Alert variant="destructive">
              <AlertDescription>{errorMessage(test.error)}</AlertDescription>
            </Alert>
          )}
          {test.data && (
            <section
              aria-label="Test result"
              className="flex min-w-0 flex-col gap-2"
            >
              <Badge
                variant={test.data.status < 400 ? "secondary" : "destructive"}
              >
                Status {test.data.status}
              </Badge>
              <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs wrap-anywhere whitespace-pre-wrap">
                {test.data.body || "Empty response"}
              </pre>
            </section>
          )}
        </form>
      </CardContent>
    </Card>
  )
}

export default function HTTPToolPage() {
  const { connectionId, toolName } = useParams()
  const { connections, workspace } = useMCPPage()
  const connection = connections.find((item) => item.id === connectionId)
  const tool = connection?.tools.find((item) => item.name === toolName)
  if (!connection || connection.kind !== "http" || (toolName && !tool))
    return (
      <>
        <Link
          to={connectorsPath(workspace.id)}
          className="text-sm underline underline-offset-4"
        >
          Back to connectors
        </Link>
        <ErrorCard
          title={connection ? "Tool not found" : "Connector not found"}
        />
      </>
    )
  return (
    <ToolEditor
      key={`${connection.id}:${toolName ?? ""}:${tool?.request ? JSON.stringify(tool.request) : ""}`}
      connection={connection}
      tool={tool}
    />
  )
}
