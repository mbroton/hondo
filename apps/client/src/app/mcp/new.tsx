import { useId, useState } from "react"
import { Link, useNavigate, useSearchParams } from "react-router"
import { RiArrowLeftLine } from "@remixicon/react"
import { PageHeading } from "@/components/page-heading"
import { Alert, AlertDescription } from "@/components/ui/alert"
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
import { Skeleton } from "@/components/ui/skeleton"
import { useBuiltinConnectors, useMCPAction } from "@/lib/api/mcp"
import type { BuiltinConnector, MCPKeyHeader } from "@/lib/api/types"
import { BuiltinFields } from "./builtin-connect"
import { connectorsPath, setupPath, useMCPPage } from "./context"
import {
  builtinErrorMessage,
  errorMessage,
  filledInputs,
  keyErrorMessage,
  type BuiltinValues,
} from "./helpers"
import { HeaderRows } from "./key-connect"

const CUSTOM = "custom"
const HTTP = "http"

export default function NewMCPPage() {
  const { workspace } = useMCPPage()
  const [params] = useSearchParams()
  const type = params.get("type")
  const catalog = useBuiltinConnectors(workspace.id)
  const builtin = catalog.data?.connectors.find((item) => item.id === type)
  return (
    <>
      <Link
        to={connectorsPath(workspace.id)}
        className="flex w-fit items-center gap-2 text-sm text-muted-foreground hover:text-foreground"
      >
        <RiArrowLeftLine className="size-4" />
        Connectors
      </Link>
      <PageHeading
        title="Add connector"
        description={`Connect a service to Hondo in ${workspace.name}.`}
      />
      {type === CUSTOM ? (
        <CustomForm />
      ) : type === HTTP ? (
        <HTTPForm />
      ) : builtin ? (
        <BuiltinForm key={builtin.id} builtin={builtin} />
      ) : type && catalog.isPending ? (
        <Skeleton className="h-96 w-full max-w-2xl" />
      ) : (
        <div className="flex flex-col gap-4">
          {catalog.isError && (
            <Alert variant="destructive">
              <AlertDescription className="flex flex-wrap items-center justify-between gap-3">
                <span>Could not load the built-in connectors.</span>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={catalog.isFetching}
                  onClick={() => void catalog.refetch()}
                >
                  {catalog.isFetching ? "Retrying..." : "Retry"}
                </Button>
              </AlertDescription>
            </Alert>
          )}
          <ul className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {catalog.isPending ? (
              <li>
                <Skeleton className="h-28 w-full" />
              </li>
            ) : (
              catalog.data?.connectors.map((item) => (
                <Choice
                  key={item.id}
                  type={item.id}
                  title={item.title}
                  description={item.description}
                />
              ))
            )}
            <Choice
              type={CUSTOM}
              title="Custom MCP server"
              description="Connect a remote MCP server from its URL."
            />
            <Choice
              type={HTTP}
              title="HTTP API"
              description="Make requests to an API into tools, one request for each tool."
            />
          </ul>
        </div>
      )}
    </>
  )
}

function Choice({
  type,
  title,
  description,
}: {
  type: string
  title: string
  description: string
}) {
  const id = useId()
  return (
    <li>
      <Link
        to={{ search: `?type=${type}` }}
        aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-description`}
        className="block h-full rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <Card className="h-full hover:bg-muted/50">
          <CardHeader>
            <CardTitle id={`${id}-title`}>{title}</CardTitle>
            <CardDescription id={`${id}-description`}>
              {description}
            </CardDescription>
          </CardHeader>
        </Card>
      </Link>
    </li>
  )
}

function BackButton({ disabled }: { disabled: boolean }) {
  const { workspace } = useMCPPage()
  const navigate = useNavigate()
  return (
    <Button
      type="button"
      variant="outline"
      onClick={() => navigate(connectorsPath(workspace.id, "new"))}
      disabled={disabled}
    >
      Back
    </Button>
  )
}

function BuiltinForm({ builtin }: { builtin: BuiltinConnector }) {
  const { workspace } = useMCPPage()
  const navigate = useNavigate()
  const id = useId()
  const [name, setName] = useState(builtin.title)
  const [values, setValues] = useState<BuiltinValues>({})
  const [consent, setConsent] = useState(false)
  const action = useMCPAction(workspace.id)
  const inputs = filledInputs(builtin, values)
  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle>{builtin.title}</CardTitle>
        <CardDescription>{builtin.description}</CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-6"
          onSubmit={async (event) => {
            event.preventDefault()
            if (!inputs || !name.trim()) return
            try {
              const result = await action.mutateAsync({
                type: "builtin",
                body: { builtin: builtin.id, name: name.trim(), inputs },
              })
              if (!result) return
              navigate(setupPath(workspace.id, result.id), { replace: true })
            } catch {
              return
            }
          }}
        >
          <BuiltinFields
            builtin={builtin}
            values={values}
            onValuesChange={setValues}
            consent={consent}
            onConsentChange={setConsent}
            disabled={action.isPending}
          >
            <Field>
              <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
              <Input
                id={`${id}-name`}
                required
                maxLength={100}
                value={name}
                onChange={(event) => setName(event.target.value)}
                disabled={action.isPending}
              />
            </Field>
          </BuiltinFields>
          {action.isError && (
            <Alert variant="destructive">
              <AlertDescription>
                {builtinErrorMessage(action.error)}
              </AlertDescription>
            </Alert>
          )}
          <div className="flex flex-wrap justify-end gap-2">
            <BackButton disabled={action.isPending} />
            <Button
              type="submit"
              disabled={action.isPending || !consent || !inputs || !name.trim()}
            >
              {action.isPending ? "Connecting…" : "Connect"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

function CustomForm() {
  const { workspace } = useMCPPage()
  const navigate = useNavigate()
  const id = useId()
  const [name, setName] = useState("")
  const [url, setUrl] = useState("")
  const action = useMCPAction(workspace.id)
  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle>Custom MCP server</CardTitle>
        <CardDescription>
          Connect a remote MCP server from its URL.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-6"
          onSubmit={async (event) => {
            event.preventDefault()
            try {
              const result = await action.mutateAsync({
                type: "create",
                body: { name, url },
              })
              if (result)
                navigate(setupPath(workspace.id, result.id), { replace: true })
            } catch {
              return
            }
          }}
        >
          {action.isError && (
            <Alert variant="destructive">
              <AlertDescription>{errorMessage(action.error)}</AlertDescription>
            </Alert>
          )}
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
              <Input
                id={`${id}-name`}
                required
                maxLength={100}
                value={name}
                onChange={(event) => setName(event.target.value)}
                disabled={action.isPending}
                autoFocus
              />
            </Field>
            <Field>
              <FieldLabel htmlFor={`${id}-url`}>Server URL</FieldLabel>
              <Input
                id={`${id}-url`}
                required
                type="url"
                maxLength={2048}
                placeholder="https://example.com/mcp"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                disabled={action.isPending}
              />
              <FieldDescription>
                Use a public HTTPS URL from a server you trust. You will sign in
                or enter a key on the next page.
              </FieldDescription>
            </Field>
          </FieldGroup>
          <div className="flex flex-wrap justify-end gap-2">
            <BackButton disabled={action.isPending} />
            <Button
              type="submit"
              disabled={action.isPending || !name.trim() || !url.trim()}
            >
              {action.isPending ? "Adding…" : "Add connector"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}

function HTTPForm() {
  const { workspace } = useMCPPage()
  const navigate = useNavigate()
  const id = useId()
  const [name, setName] = useState("")
  const [url, setUrl] = useState("")
  const [headers, setHeaders] = useState<MCPKeyHeader[]>([])
  const action = useMCPAction(workspace.id)
  const complete =
    name.trim() &&
    url.trim() &&
    headers.every((header) => header.name.trim() && header.value.trim())
  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <CardTitle>HTTP API</CardTitle>
        <CardDescription>
          Each tool sends one request that you define to this API.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-6"
          onSubmit={async (event) => {
            event.preventDefault()
            if (!complete) return
            try {
              const result = await action.mutateAsync({
                type: "http",
                body: { name, url, headers },
              })
              if (result)
                navigate(setupPath(workspace.id, result.id), { replace: true })
            } catch {
              return
            }
          }}
        >
          {action.isError && (
            <Alert variant="destructive">
              <AlertDescription>
                {keyErrorMessage({ headers }, action.error)}
              </AlertDescription>
            </Alert>
          )}
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor={`${id}-name`}>Name</FieldLabel>
              <Input
                id={`${id}-name`}
                required
                maxLength={100}
                value={name}
                onChange={(event) => setName(event.target.value)}
                disabled={action.isPending}
                autoFocus
              />
            </Field>
            <Field>
              <FieldLabel htmlFor={`${id}-url`}>API URL</FieldLabel>
              <Input
                id={`${id}-url`}
                required
                type="url"
                maxLength={2048}
                placeholder="https://api.example.com"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                disabled={action.isPending}
              />
              <FieldDescription>
                Each tool sends its request to this host and nowhere else.
              </FieldDescription>
            </Field>
            <FieldSet className="gap-3" disabled={action.isPending}>
              <FieldLegend variant="label">Headers</FieldLegend>
              <FieldDescription>
                Sent with each request, for example an Authorization header with
                a key. Hondo encrypts the values and never shows them again.
              </FieldDescription>
              <HeaderRows headers={headers} min={0} onChange={setHeaders} />
            </FieldSet>
          </FieldGroup>
          <div className="flex flex-wrap justify-end gap-2">
            <BackButton disabled={action.isPending} />
            <Button type="submit" disabled={action.isPending || !complete}>
              {action.isPending ? "Adding…" : "Add connector"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  )
}
