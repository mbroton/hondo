import { useSearchParams } from "react-router"
import { RiSlackFill } from "@remixicon/react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { slackInstallUrl } from "@/lib/auth"
import { installErrorMessage } from "@/lib/install-errors"

export function SlackInstallButton({
  reinstall = false,
  variant = "default",
  size = "default",
  className,
}: {
  reinstall?: boolean
  variant?: "default" | "outline"
  size?: "default" | "sm" | "lg"
  className?: string
}) {
  return (
    <Button
      variant={variant}
      size={size}
      className={className}
      nativeButton={false}
      render={<a href={slackInstallUrl()} />}
    >
      <RiSlackFill data-icon="inline-start" />
      {reinstall ? "Reinstall in Slack" : "Add to Slack"}
    </Button>
  )
}

/** Reports how the last install attempt ended, from the callback's redirect. */
export function SlackInstallNotice() {
  const [searchParams] = useSearchParams()
  const errorCode = searchParams.get("slack_error")

  if (errorCode !== null) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{installErrorMessage(errorCode)}</AlertDescription>
      </Alert>
    )
  }

  if (searchParams.get("installed") === null) {
    return null
  }

  return (
    <Alert role="status">
      <AlertDescription>Hondo is installed in Slack.</AlertDescription>
    </Alert>
  )
}
