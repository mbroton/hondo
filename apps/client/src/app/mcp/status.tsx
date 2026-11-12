import {
  RiCheckboxCircleFill,
  RiErrorWarningFill,
  RiPauseCircleLine,
  RiRadioButtonLine,
  RiSubtractLine,
} from "@remixicon/react"
import type { MCPConnection } from "@/lib/api/types"
import { cn } from "@/lib/utils"

export function ConnectionStatus({
  connection,
}: {
  connection: MCPConnection
}) {
  let label = "Connected"
  let Icon = RiRadioButtonLine
  let color = "text-success"
  if (!connection.connected) {
    label = "Not connected"
    Icon = RiSubtractLine
    color = "text-muted-foreground"
  } else if (connection.status === "sign_in_required") {
    label = connection.auth === "key" ? "Key refused" : "Sign-in required"
    Icon = RiErrorWarningFill
    color = "text-warning"
  } else if (connection.status === "unavailable") {
    label = "Unavailable"
    Icon = RiErrorWarningFill
    color = "text-destructive"
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-sm sm:whitespace-nowrap">
      <Icon className={cn("size-4 shrink-0", color)} aria-hidden="true" />
      {label}
    </span>
  )
}

export function AccessStatus({ enabled }: { enabled: boolean }) {
  return (
    <span className="inline-flex items-center gap-1.5 text-sm">
      {enabled ? (
        <RiCheckboxCircleFill
          className="size-4 shrink-0 text-success"
          aria-hidden="true"
        />
      ) : (
        <RiPauseCircleLine
          className="size-4 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
      )}
      {enabled ? "Enabled" : "Disabled"}
    </span>
  )
}
