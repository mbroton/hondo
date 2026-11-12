import type { ReactNode } from "react"
import { Link } from "react-router"
import {
  RiCheckboxBlankCircleLine,
  RiCheckboxCircleFill,
  RiSlackFill,
} from "@remixicon/react"

import { connectorsPath } from "@/app/mcp/context"
import {
  SlackInstallButton,
  SlackInstallNotice,
} from "@/components/add-to-slack"
import { ErrorCard } from "@/components/error-card"
import { PageHeading } from "@/components/page-heading"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { useWorkspace } from "@/hooks/use-workspace"
import { useSetup } from "@/lib/api/queries"
import type { Setup, Workspace } from "@/lib/api/types"
import { cn } from "@/lib/utils"

type Step = {
  title: string
  description: string
  done: boolean
  action:
    | { label: string; to: string }
    | { label: string; slack: true }
    | { reinstall: true }
    | null
}

// The runtime serves only an active workspace; a reinstall makes it active.
function isActive(workspace: Workspace): boolean {
  return workspace.status === "active"
}

function slackUrl(workspace: Workspace): string {
  return `https://app.slack.com/client/${workspace.slack_team_id}`
}

function steps(workspace: Workspace, setup: Setup): Step[] {
  return [
    {
      title: "Add Hondo to Slack",
      description: isActive(workspace)
        ? `Hondo is in ${workspace.name}.`
        : `Hondo is paused in ${workspace.name}. Reinstall it to turn it back on.`,
      done: isActive(workspace),
      action: { reinstall: true },
    },
    {
      title: "Connect an AI provider",
      description: "Add the API key that Hondo uses to answer.",
      done: setup.provider_connected,
      action: { label: "Add a key", to: "/settings" },
    },
    {
      title: "Choose a model",
      description: "Pick the model that answers your team.",
      done: setup.model_selected,
      action: { label: "Choose a model", to: "/settings" },
    },
    {
      title: "Connect a tool",
      description: "Let Hondo look things up in the tools your team uses.",
      done: setup.tool_connected,
      action: {
        label: "Connect a tool",
        to: connectorsPath(workspace.id),
      },
    },
    {
      title: "Ask @Hondo in a channel",
      description:
        "In Slack, type /invite @Hondo in a channel where your team works, then mention @Hondo with a question.",
      done: setup.answered_in_channel,
      action: { label: "Open Slack", slack: true },
    },
  ]
}

function StepAction({
  action,
  workspace,
  next,
}: {
  action: NonNullable<Step["action"]>
  workspace: Workspace
  next: boolean
}) {
  const variant = next ? "default" : "outline"
  if ("reinstall" in action) {
    return <SlackInstallButton reinstall size="sm" variant={variant} />
  }
  if ("to" in action) {
    return (
      <Button
        size="sm"
        variant={variant}
        nativeButton={false}
        render={<Link to={action.to} />}
      >
        {action.label}
      </Button>
    )
  }
  return (
    <Button
      size="sm"
      variant={variant}
      nativeButton={false}
      render={<a href={slackUrl(workspace)} target="_blank" rel="noreferrer" />}
    >
      <RiSlackFill data-icon="inline-start" />
      {action.label}
    </Button>
  )
}

function Checklist({
  workspace,
  items,
}: {
  workspace: Workspace
  items: Step[]
}) {
  const next = items.find((step) => !step.done)
  return (
    <ol
      className="divide-y overflow-hidden rounded-xl border"
      aria-label="Setup steps"
    >
      {items.map((step) => (
        <li
          key={step.title}
          className={cn(
            "flex flex-wrap items-start gap-x-4 gap-y-3 px-4 py-4 sm:flex-nowrap sm:items-center",
            step === next && "bg-muted/60"
          )}
        >
          {step.done ? (
            <RiCheckboxCircleFill
              className="size-5 shrink-0 text-success"
              aria-hidden="true"
            />
          ) : (
            <RiCheckboxBlankCircleLine
              className="size-5 shrink-0 text-muted-foreground"
              aria-hidden="true"
            />
          )}
          <div className="flex min-w-0 flex-1 flex-col gap-0.5">
            <p
              className={cn(
                "text-sm font-medium",
                step.done && "text-muted-foreground"
              )}
            >
              {step.title}
              <span className="sr-only">
                {step.done ? ", done" : ", to do"}
              </span>
            </p>
            <p className="text-sm leading-6 text-muted-foreground">
              {step.description}
            </p>
          </div>
          {!step.done && step.action && (
            <div className="w-full pl-9 sm:w-auto sm:pl-0">
              <StepAction
                action={step.action}
                workspace={workspace}
                next={step === next}
              />
            </div>
          )}
        </li>
      ))}
    </ol>
  )
}

function Tip({ title, children }: { title: string; children: ReactNode }) {
  return (
    <li className="flex flex-col gap-1 px-4 py-4">
      <h2 className="text-sm font-medium">{title}</h2>
      <p className="text-sm leading-6 text-muted-foreground">{children}</p>
    </li>
  )
}

function Ready({ workspace }: { workspace: Workspace }) {
  const linkClass =
    "font-medium text-foreground underline underline-offset-4 hover:no-underline"
  return (
    <ul
      className="divide-y overflow-hidden rounded-xl border"
      aria-label="Tips"
    >
      <Tip title="Keep a conversation in its thread">
        Mention @Hondo with a question. Reply in the same thread, so Hondo keeps
        the context together.
      </Tip>
      <Tip title="Connect more tools">
        Each tool you connect gives Hondo more places to look.{" "}
        <Link to={connectorsPath(workspace.id)} className={linkClass}>
          Open Connectors
        </Link>
      </Tip>
      <Tip title="Follow the work">
        <Link to="/activity" className={linkClass}>
          Activity
        </Link>{" "}
        shows the conversations that run now.{" "}
        <Link to="/usage" className={linkClass}>
          Usage
        </Link>{" "}
        shows model requests and costs.
      </Tip>
    </ul>
  )
}

export default function HomePage() {
  const selection = useWorkspace()
  const { workspace } = selection
  const setup = useSetup(workspace?.id ?? null)
  const data = setup.data

  if (selection.isPending || (workspace && setup.isPending)) {
    return (
      <div role="status" className="flex w-full max-w-3xl flex-col gap-7">
        <span className="sr-only">Loading setup</span>
        <Skeleton className="h-9 w-56" />
        <Skeleton className="h-96 w-full" />
      </div>
    )
  }

  if (!workspace || !data) {
    return (
      <div className="flex w-full max-w-3xl flex-col gap-7">
        <PageHeading title="Home" description="Set up and follow Hondo." />
        <SlackInstallNotice />
        <ErrorCard
          title="Could not load the setup steps"
          onRetry={workspace ? () => void setup.refetch() : undefined}
          retrying={setup.isFetching}
        />
      </div>
    )
  }

  const items = steps(workspace, data)
  const done = items.filter((step) => step.done).length
  const ready = data.complete && isActive(workspace)

  return (
    <div className="flex w-full max-w-3xl flex-col gap-7">
      {ready ? (
        <PageHeading
          title="Hondo is ready"
          description={`Your team can ask @Hondo in ${workspace.name}.`}
        >
          <Button
            variant="outline"
            nativeButton={false}
            render={
              <a href={slackUrl(workspace)} target="_blank" rel="noreferrer" />
            }
          >
            <RiSlackFill data-icon="inline-start" />
            Open Slack
          </Button>
        </PageHeading>
      ) : (
        <PageHeading
          title="Set up Hondo"
          description={
            !isActive(workspace)
              ? `Hondo doesn’t answer in ${workspace.name} until you reinstall it in Slack.`
              : data.model_selected
                ? `Hondo can answer in ${workspace.name} now. The open steps help your team get more from it.`
                : `Hondo answers in ${workspace.name} after you connect a provider and choose a model.`
          }
        >
          <Badge variant="secondary">
            {done} of {items.length} done
          </Badge>
        </PageHeading>
      )}
      <SlackInstallNotice />
      {ready ? (
        <Ready workspace={workspace} />
      ) : (
        <Checklist workspace={workspace} items={items} />
      )}
    </div>
  )
}
