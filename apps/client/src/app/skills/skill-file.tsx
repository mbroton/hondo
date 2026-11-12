import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import { Textarea } from "@/components/ui/textarea"

const MAX_LENGTH = 20_000

const example = `---
name: incident-report
description: Write an incident report from a Slack thread. Use it when someone asks for a report.
---

1. Read the whole thread.
2. List the impact, the cause and the fix.`

export function SkillFile({
  id,
  value,
  onChange,
  disabled,
  autoFocus,
}: {
  id: string
  value: string
  onChange: (value: string) => void
  disabled: boolean
  autoFocus?: boolean
}) {
  return (
    <Field>
      <FieldLabel htmlFor={id}>SKILL.md</FieldLabel>
      <Textarea
        id={id}
        required
        maxLength={MAX_LENGTH}
        spellCheck={false}
        placeholder={example}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        disabled={disabled}
        autoFocus={autoFocus}
        className="max-h-[36rem] min-h-72 font-mono"
      />
      <FieldDescription>
        Start the file with YAML frontmatter that has a name and a description.
        Write the instructions after it. See the{" "}
        <a
          href="https://agentskills.io/specification"
          target="_blank"
          rel="noreferrer"
        >
          Agent Skills specification
        </a>
        .
      </FieldDescription>
      <FieldDescription>
        {value.length.toLocaleString("en-US")} of{" "}
        {MAX_LENGTH.toLocaleString("en-US")} characters
      </FieldDescription>
    </Field>
  )
}
