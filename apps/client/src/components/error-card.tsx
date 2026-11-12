import { RiErrorWarningLine, RiRefreshLine } from "@remixicon/react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"

export function ErrorCard({
  title,
  description = "This information could not be loaded. Try again.",
  onRetry,
  retrying = false,
}: {
  title: string
  description?: string
  onRetry?: () => void
  retrying?: boolean
}) {
  return (
    <Alert>
      <RiErrorWarningLine aria-hidden="true" />
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription className="flex flex-col items-start gap-4">
        <p>{description}</p>
        <Button
          variant="outline"
          disabled={retrying}
          onClick={onRetry ?? (() => window.location.reload())}
        >
          <RiRefreshLine data-icon="inline-start" />
          {retrying ? "Trying again…" : "Try again"}
        </Button>
      </AlertDescription>
    </Alert>
  )
}
