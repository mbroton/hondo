import type { ReactNode } from "react"
import { Navigate, useLocation } from "react-router"

import { WorkspaceAdmins } from "@/app/settings/admins/page"
import { AgentSettings } from "@/app/settings/agent/page"
import { ListeningSettings } from "@/app/settings/listening/page"
import { WorkspaceProviders } from "@/app/settings/providers/page"
import { MentionOnlySettings } from "@/app/settings/replies/page"
import { SlackInstallButton } from "@/components/add-to-slack"
import { ErrorCard } from "@/components/error-card"
import { PageHeading } from "@/components/page-heading"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { useWorkspace } from "@/hooks/use-workspace"
import { cn } from "@/lib/utils"

export function LegacySettingsRedirect() {
  const { search } = useLocation()
  return <Navigate to={`/settings${search}`} replace />
}

function SettingsSection({
  id,
  title,
  description,
  aside,
  first = false,
  children,
}: {
  id: string
  title: string
  description: string
  aside?: ReactNode
  first?: boolean
  children: ReactNode
}) {
  return (
    <section
      id={id}
      aria-labelledby={`${id}-heading`}
      className={cn(
        "grid scroll-mt-20 gap-5 lg:grid-cols-[13rem_minmax(0,1fr)] lg:gap-8",
        !first && "border-t pt-10"
      )}
    >
      <div className="flex flex-col items-start gap-3">
        <h2 id={`${id}-heading`} className="text-base font-semibold">
          {title}
        </h2>
        {aside}
        <p className="text-sm leading-6 text-muted-foreground">{description}</p>
      </div>
      {children}
    </section>
  )
}

export default function SettingsPage() {
  const selection = useWorkspace()
  const { workspace } = selection

  return (
    <div className="flex w-full max-w-5xl flex-col gap-10">
      <PageHeading
        title="Settings"
        description="Manage the AI provider, the model, replies, listening, the admins, and the Slack install."
      />
      {selection.isPending ? (
        <Skeleton className="h-64 w-full" />
      ) : selection.isError || !workspace ? (
        <ErrorCard title="Could not load the workspace" />
      ) : (
        <>
          <SettingsSection
            id="connections"
            title="Connections"
            description="Provider keys that Hondo uses in this workspace."
            first
          >
            <WorkspaceProviders key={workspace.id} workspaceId={workspace.id} />
          </SettingsSection>
          <SettingsSection
            id="model"
            title="Model"
            description="The model Hondo uses when it answers in Slack."
          >
            <AgentSettings key={workspace.id} workspace={workspace} />
          </SettingsSection>
          <SettingsSection
            id="replies"
            title="Replies"
            description="In these channels, Hondo answers only messages that mention it."
          >
            <MentionOnlySettings
              key={workspace.id}
              workspaceId={workspace.id}
            />
          </SettingsSection>
          <SettingsSection
            id="listening"
            title="Listening"
            description="In these channels, Hondo also reads messages that do not mention it. It answers when the goal applies."
          >
            <ListeningSettings key={workspace.id} workspaceId={workspace.id} />
          </SettingsSection>
          <SettingsSection
            id="admins"
            title="Admins"
            description="Admins open this dashboard and change its settings. Slack workspace owners and admins also have access."
          >
            <WorkspaceAdmins key={workspace.id} workspaceId={workspace.id} />
          </SettingsSection>
          <SettingsSection
            id="slack-workspace"
            title="Slack workspace"
            description="Reinstall when Hondo needs new Slack permissions. Your settings stay the same."
            aside={
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-sm font-medium">{workspace.name}</p>
                {workspace.status !== "active" && (
                  <Badge variant="destructive">{workspace.status}</Badge>
                )}
              </div>
            }
          >
            <div className="flex items-start">
              <SlackInstallButton reinstall variant="outline" />
            </div>
          </SettingsSection>
        </>
      )}
    </div>
  )
}
