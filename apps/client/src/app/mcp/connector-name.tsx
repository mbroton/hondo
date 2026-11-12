import { useRef, useState } from "react"
import { RiEditLine } from "@remixicon/react"
import { Input } from "@/components/ui/input"

export function ConnectorName({
  name,
  disabled,
  onChange,
}: {
  name: string
  disabled: boolean
  onChange: (name: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(name)
  const button = useRef<HTMLButtonElement>(null)

  function finish(restoreFocus: boolean) {
    setEditing(false)
    if (restoreFocus) requestAnimationFrame(() => button.current?.focus())
  }

  return editing ? (
    <div className="flex w-full max-w-lg flex-col gap-1">
      <h1 className="sr-only">{name}</h1>
      <Input
        aria-label="Connector name"
        value={draft}
        disabled={disabled}
        maxLength={100}
        autoFocus
        onFocus={(event) => event.currentTarget.select()}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => {
          if (draft.trim()) onChange(draft.trim())
          finish(false)
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault()
            if (draft.trim()) {
              onChange(draft.trim())
              finish(true)
            }
          } else if (event.key === "Escape") {
            event.preventDefault()
            finish(true)
          }
        }}
      />
      <p className="text-xs text-muted-foreground">
        Enter to apply · Escape to cancel
      </p>
    </div>
  ) : (
    <h1
      aria-label={name}
      className="min-w-0 text-2xl font-semibold tracking-tight sm:text-3xl"
    >
      {disabled ? (
        name
      ) : (
        <button
          ref={button}
          type="button"
          aria-label={`Rename ${name}`}
          className="group flex max-w-full items-center gap-2 rounded-sm text-left outline-none hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring"
          onClick={() => {
            setDraft(name)
            setEditing(true)
          }}
        >
          <span className="wrap-anywhere">{name}</span>
          <RiEditLine
            className="size-4 shrink-0 text-muted-foreground"
            aria-hidden="true"
          />
        </button>
      )}
    </h1>
  )
}
