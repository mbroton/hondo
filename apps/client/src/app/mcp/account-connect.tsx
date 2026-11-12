import { useId, useState } from "react"
import type { UseQueryResult } from "@tanstack/react-query"
import { ErrorCard } from "@/components/error-card"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { apiFetch } from "@/lib/api/client"
import type {
  BuiltinConnector,
  BuiltinConnectors,
  MCPConnection,
  MCPKeyHeader,
  MCPOAuthRedirect,
} from "@/lib/api/types"
import { BuiltinConnect } from "./builtin-connect"
import { errorMessage, type BuiltinValues } from "./helpers"
import { KeyConnect } from "./key-connect"

type Method = NonNullable<MCPConnection["auth"]>

export type AccountKey = { headers: MCPKeyHeader[] } | { inputs: BuiltinValues }

export function AccountConnect({
  workspaceId,
  connection,
  builtin,
  pending,
  onSaveKey,
}: {
  workspaceId: string
  connection: MCPConnection
  builtin?: BuiltinConnector
  pending: boolean
  onSaveKey: (key: AccountKey) => Promise<void>
}) {
  const [method, setMethod] = useState<Method>(connection.auth ?? "oauth")
  const [signingIn, setSigningIn] = useState(false)
  const refused =
    connection.auth === "key" && connection.status === "sign_in_required"
  if (builtin) {
    return (
      <div className="flex max-w-2xl flex-col gap-4">
        {refused && (
          <Alert variant="destructive">
            <AlertDescription>
              The service refused this token. Check the token and its
              permissions, then save it again.
            </AlertDescription>
          </Alert>
        )}
        <BuiltinConnect
          builtin={builtin}
          pending={pending}
          onSave={(inputs) => onSaveKey({ inputs })}
        />
      </div>
    )
  }
  return (
    <div className="flex max-w-2xl flex-col gap-4">
      <ToggleGroup
        variant="outline"
        spacing={0}
        aria-label="Connection method"
        disabled={pending || signingIn}
        value={[method]}
        onValueChange={([next]) => {
          if (next === "oauth" || next === "key") setMethod(next)
        }}
      >
        <ToggleGroupItem value="oauth">Sign in</ToggleGroupItem>
        <ToggleGroupItem value="key">Key</ToggleGroupItem>
      </ToggleGroup>
      {method === "key" && refused && (
        <Alert variant="destructive">
          <AlertDescription>
            The server refused this key. Check the header names and values, then
            save the key again.
          </AlertDescription>
        </Alert>
      )}
      {method === "oauth" ? (
        <OAuthConnect
          workspaceId={workspaceId}
          connection={connection}
          pending={signingIn}
          setPending={setSigningIn}
        />
      ) : (
        <KeyConnect
          pending={pending}
          onSave={(headers) => onSaveKey({ headers })}
        />
      )}
    </div>
  )
}

export function MissingBuiltinSetup({
  catalog,
}: {
  catalog: UseQueryResult<BuiltinConnectors>
}) {
  if (catalog.isPending) return <Skeleton className="h-64 w-full max-w-2xl" />
  if (catalog.isError)
    return (
      <ErrorCard
        title="Could not load the setup for this connector"
        onRetry={() => void catalog.refetch()}
        retrying={catalog.isFetching}
      />
    )
  return (
    <Alert className="max-w-2xl">
      <AlertTitle>This built-in connector is no longer available</AlertTitle>
      <AlertDescription>
        Delete this connector. Then add the service again from Add connector.
      </AlertDescription>
    </Alert>
  )
}

function OAuthConnect({
  workspaceId,
  connection,
  pending,
  setPending,
}: {
  workspaceId: string
  connection: MCPConnection
  pending: boolean
  setPending: (pending: boolean) => void
}) {
  const id = useId()
  const [consent, setConsent] = useState(false)
  const [advanced, setAdvanced] = useState(false)
  const [clientId, setClientId] = useState("")
  const [clientSecret, setClientSecret] = useState("")
  const [scope, setScope] = useState("")
  const [error, setError] = useState<string | null>(null)
  return (
    <div className="flex flex-col gap-4">
      <Field orientation="horizontal">
        <Checkbox
          id={`${id}-consent`}
          checked={consent}
          onCheckedChange={setConsent}
          disabled={pending}
        />
        <FieldLabel htmlFor={`${id}-consent`}>
          I authorize this account's access for the workspace agent.
        </FieldLabel>
      </Field>
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="w-fit"
        onClick={() => setAdvanced(!advanced)}
        aria-expanded={advanced}
      >
        Advanced setup
      </Button>
      {advanced && (
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor={`${id}-client`}>OAuth client ID</FieldLabel>
            <Input
              id={`${id}-client`}
              value={clientId}
              onChange={(event) => setClientId(event.target.value)}
              maxLength={2048}
            />
            <FieldDescription>
              Only needed when the provider requires a registered OAuth app.
            </FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor={`${id}-secret`}>
              Client secret (optional)
            </FieldLabel>
            <Input
              id={`${id}-secret`}
              type="password"
              autoComplete="new-password"
              value={clientSecret}
              onChange={(event) => setClientSecret(event.target.value)}
              maxLength={8192}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor={`${id}-scope`}>Scopes (optional)</FieldLabel>
            <Input
              id={`${id}-scope`}
              value={scope}
              onChange={(event) => setScope(event.target.value)}
              maxLength={2048}
            />
            <FieldDescription>
              Leave blank to use the server's requested access. Use spaces to
              separate scopes.
            </FieldDescription>
          </Field>
        </FieldGroup>
      )}
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <Button
        type="button"
        className="w-fit"
        disabled={!consent || pending}
        onClick={async () => {
          setPending(true)
          setError(null)
          try {
            const result = await apiFetch<MCPOAuthRedirect>(
              `/workspaces/${workspaceId}/mcp-connections/${connection.id}/authorize`,
              {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  client_id: clientId.trim() || undefined,
                  client_secret: clientSecret || undefined,
                  scope: scope.trim() || undefined,
                }),
              }
            )
            window.location.assign(result.url)
          } catch (cause) {
            setError(errorMessage(cause))
            setPending(false)
          }
        }}
      >
        {pending ? "Connecting…" : "Connect account"}
      </Button>
    </div>
  )
}
