import { useId, useState, type ReactNode } from "react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field"
import { Input } from "@/components/ui/input"
import type { BuiltinConnector } from "@/lib/api/types"
import { filledInputs, type BuiltinValues } from "./helpers"

export function BuiltinFields({
  builtin,
  values,
  onValuesChange,
  consent,
  onConsentChange,
  disabled,
  children,
}: {
  builtin: BuiltinConnector
  values: BuiltinValues
  onValuesChange: (values: BuiltinValues) => void
  consent: boolean
  onConsentChange: (consent: boolean) => void
  disabled: boolean
  children?: ReactNode
}) {
  const id = useId()
  return (
    <FieldGroup>
      {children}
      <div className="flex flex-col gap-2">
        <p id={`${id}-setup`} className="text-sm font-medium">
          Before you connect
        </p>
        <ol
          aria-labelledby={`${id}-setup`}
          className="flex list-decimal flex-col gap-1.5 pl-5 text-sm text-muted-foreground"
        >
          {builtin.setup.map((step, index) => (
            <li key={index}>{step}</li>
          ))}
        </ol>
      </div>
      {builtin.inputs.map((input) => (
        <Field key={input.name}>
          <FieldLabel htmlFor={`${id}-${input.name}`}>{input.label}</FieldLabel>
          <Input
            id={`${id}-${input.name}`}
            type={input.secret ? "password" : "text"}
            autoComplete={input.secret ? "new-password" : "off"}
            spellCheck={false}
            required
            maxLength={8192}
            value={values[input.name] ?? ""}
            onChange={(event) =>
              onValuesChange({ ...values, [input.name]: event.target.value })
            }
            disabled={disabled}
          />
          {input.help && <FieldDescription>{input.help}</FieldDescription>}
        </Field>
      ))}
      <p className="text-sm text-muted-foreground">
        Hondo encrypts what you enter here and never shows it again.
      </p>
      <Field orientation="horizontal">
        <Checkbox
          id={`${id}-consent`}
          checked={consent}
          onCheckedChange={onConsentChange}
          disabled={disabled}
        />
        <FieldLabel htmlFor={`${id}-consent`}>
          I authorize this key's access for the workspace agent.
        </FieldLabel>
      </Field>
    </FieldGroup>
  )
}

export function BuiltinConnect({
  builtin,
  pending,
  onSave,
}: {
  builtin: BuiltinConnector
  pending: boolean
  onSave: (inputs: BuiltinValues) => Promise<void>
}) {
  const [values, setValues] = useState<BuiltinValues>({})
  const [consent, setConsent] = useState(false)
  const inputs = filledInputs(builtin, values)
  return (
    <form
      className="flex flex-col gap-6"
      onSubmit={(event) => {
        event.preventDefault()
        if (inputs) void onSave(inputs)
      }}
    >
      <BuiltinFields
        builtin={builtin}
        values={values}
        onValuesChange={setValues}
        consent={consent}
        onConsentChange={setConsent}
        disabled={pending}
      />
      <Button
        type="submit"
        className="w-fit"
        disabled={!consent || !inputs || pending}
      >
        {pending ? "Saving…" : "Save key"}
      </Button>
    </form>
  )
}
