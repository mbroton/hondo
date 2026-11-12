import { useId, useState } from "react"
import { RiAddLine, RiCloseLine } from "@remixicon/react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import type { MCPKeyHeader } from "@/lib/api/types"

const MAX_HEADERS = 10

export function KeyConnect({
  pending,
  onSave,
  minHeaders = 1,
}: {
  pending: boolean
  onSave: (headers: MCPKeyHeader[]) => Promise<void>
  minHeaders?: number
}) {
  const id = useId()
  const [headers, setHeaders] = useState<MCPKeyHeader[]>([
    { name: "Authorization", value: "" },
  ])
  const [consent, setConsent] = useState(false)
  const complete = headers.every(
    (header) => header.name.trim() && header.value.trim()
  )

  return (
    <form
      onSubmit={(event) => {
        event.preventDefault()
        void onSave(headers)
      }}
    >
      <FieldGroup>
        <FieldSet className="gap-3">
          <FieldLegend variant="label">Headers</FieldLegend>
          <FieldDescription>
            Hondo sends these headers with every request to this server. Enter
            the full value, for example “Bearer” and the token. For Basic auth,
            enter “Basic” and the Base64 of “email:token”.
          </FieldDescription>
          <HeaderRows headers={headers} min={minHeaders} onChange={setHeaders} />
        </FieldSet>
        <p className="text-sm text-muted-foreground">
          Use a key from a service account (a bot user), not a personal key.
          Hondo encrypts the values and never shows them again.
        </p>
        <Field orientation="horizontal">
          <Checkbox
            id={`${id}-consent`}
            checked={consent}
            onCheckedChange={setConsent}
          />
          <FieldLabel htmlFor={`${id}-consent`}>
            I authorize this key's access for the workspace agent.
          </FieldLabel>
        </Field>
        <Button
          type="submit"
          className="w-fit"
          disabled={!consent || !complete || pending}
        >
          {pending ? "Saving…" : "Save key"}
        </Button>
      </FieldGroup>
    </form>
  )
}

export function HeaderRows({
  headers,
  min,
  onChange,
}: {
  headers: MCPKeyHeader[]
  min: number
  onChange: (headers: MCPKeyHeader[]) => void
}) {
  function update(index: number, change: Partial<MCPKeyHeader>) {
    onChange(
      headers.map((header, position) =>
        position === index ? { ...header, ...change } : header
      )
    )
  }

  return (
    <>
      {headers.map((header, index) => (
        <Field orientation="horizontal" key={index}>
          <Input
            className="w-2/5"
            aria-label={`Header ${index + 1} name`}
            placeholder="Authorization"
            maxLength={100}
            value={header.name}
            onChange={(event) => update(index, { name: event.target.value })}
          />
          <Input
            className="min-w-0 flex-1"
            type="password"
            autoComplete="new-password"
            aria-label={`Header ${index + 1} value`}
            placeholder="Bearer …"
            maxLength={8192}
            value={header.value}
            onChange={(event) => update(index, { value: event.target.value })}
          />
          {headers.length > min && (
            <Button
              type="button"
              variant="ghost"
              size="icon-sm"
              aria-label={`Remove header ${index + 1}`}
              onClick={() =>
                onChange(headers.filter((_, position) => position !== index))
              }
            >
              <RiCloseLine />
            </Button>
          )}
        </Field>
      ))}
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="w-fit"
        disabled={headers.length >= MAX_HEADERS}
        onClick={() => onChange([...headers, { name: "", value: "" }])}
      >
        <RiAddLine data-icon="inline-start" />
        Add header
      </Button>
    </>
  )
}
