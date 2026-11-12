import { useSearchParams } from "react-router"
import { RiLogoutBoxLine, RiRefreshLine } from "@remixicon/react"

import { SlackInstallButton } from "@/components/add-to-slack"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { Button } from "@/components/ui/button"
import { Separator } from "@/components/ui/separator"
import { useLogout, useMe } from "@/lib/api/queries"
import type { Team } from "@/lib/api/types"
import { loginUrl } from "@/lib/auth"
import { installErrorMessage } from "@/lib/install-errors"

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  const first = parts[0]?.charAt(0) ?? ""
  const last = parts.length > 1 ? parts[parts.length - 1].charAt(0) : ""
  return (first + last).toUpperCase() || "?"
}

// Shaped like the workspace icon in Slack's switcher, so the page reads as
// being about this one workspace.
function WorkspaceTile({ name }: { name: string }) {
  return (
    <div
      aria-hidden="true"
      className="flex size-14 items-center justify-center rounded-xl bg-primary text-2xl font-semibold text-primary-foreground"
    >
      {name.trim().charAt(0).toUpperCase() || "?"}
    </div>
  )
}

function NotInstalled({ team }: { team: Team }) {
  return (
    <>
      <div className="flex flex-col gap-3">
        <h1 className="text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
          Add Hondo to {team.name}
        </h1>
        <p className="text-base leading-7 text-muted-foreground">
          Hondo isn’t in this Slack workspace yet. Add it, and you become its
          admin.
        </p>
      </div>
      <SlackInstallButton size="lg" className="w-fit" />
      <ol className="flex list-decimal flex-col gap-2 pl-5 text-sm leading-6 text-muted-foreground marker:text-foreground">
        <li className="pl-1">Slack asks you to approve Hondo.</li>
        <li className="pl-1">You connect an AI provider and choose a model.</li>
        <li className="pl-1">You invite @Hondo to a channel and ask it.</li>
      </ol>
    </>
  )
}

function AdminList({ label, names }: { label: string; names: string[] }) {
  if (names.length === 0) {
    return null
  }
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm font-medium">{label}</p>
      <ul className="flex flex-col gap-2">
        {/* Slack display names aren't unique, and this list never reorders. */}
        {names.map((name, index) => (
          <li key={`${index}-${name}`} className="flex items-center gap-3 text-sm">
            <Avatar size="sm">
              <AvatarFallback>{initials(name)}</AvatarFallback>
            </Avatar>
            <span className="truncate">{name}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// Everyone in the team sees this: without a working token nobody's Slack role
// can be checked, and the install itself refuses people who aren't admins.
function ReinstallRequired({ team }: { team: Team }) {
  return (
    <>
      <div className="flex flex-col gap-3">
        <h1 className="text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
          Hondo lost access to {team.name}
        </h1>
        <p className="text-base leading-7 text-muted-foreground">
          Slack no longer accepts Hondo’s connection, for example because Hondo
          was removed from the workspace. Hondo can’t answer in Slack until an
          admin adds it again.
        </p>
      </div>
      <AdminList label="Hondo admins:" names={team.admins} />
      <div className="flex flex-col gap-3">
        <p className="text-sm leading-6 text-muted-foreground">
          A Hondo admin, or a Slack workspace owner or admin, can restore it.
        </p>
        <SlackInstallButton size="lg" className="w-fit" />
      </div>
    </>
  )
}

function SlackUnavailable({ team }: { team: Team }) {
  const me = useMe()
  return (
    <>
      <div className="flex flex-col gap-3">
        <h1 className="text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
          Hondo can’t reach Slack
        </h1>
        <p className="text-base leading-7 text-muted-foreground">
          Hondo checks with Slack who may open the {team.name} dashboard, and
          Slack didn’t answer. Try again in a minute.
        </p>
      </div>
      <Button
        size="lg"
        className="w-fit"
        disabled={me.isFetching}
        onClick={() => void me.refetch()}
      >
        <RiRefreshLine data-icon="inline-start" />
        Try again
      </Button>
    </>
  )
}

function NoAccess({ team }: { team: Team }) {
  return (
    <>
      <div className="flex flex-col gap-3">
        <h1 className="text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
          Hondo is in {team.name}
        </h1>
        <p className="text-base leading-7 text-muted-foreground">
          Only its admins can open this dashboard. You can still mention @Hondo
          in Slack.
        </p>
      </div>
      <AdminList label="Ask an admin for access:" names={team.admins} />
      <p className="text-sm leading-6 text-muted-foreground">
        {team.admins.length > 0
          ? "Slack workspace owners and admins also have access."
          : "Slack workspace owners and admins have access. Ask one of them."}
      </p>
    </>
  )
}

export default function StartPage() {
  const { data: me } = useMe()
  const [searchParams] = useSearchParams()
  const logout = useLogout()
  const slackError = searchParams.get("slack_error")

  // Renders under RequireAuth, so the session is always present.
  if (!me) {
    return null
  }

  const { user } = me
  // Sign-in can miss the team's name; a sentence with a blank name reads broken.
  const team = { ...me.team, name: me.team.name || "your Slack workspace" }

  return (
    <main className="flex min-h-svh flex-col px-6 py-8 sm:px-12">
      <title>{`${team.name} · Hondo`}</title>
      <div className="flex items-center gap-3">
        <img src="/logo-sm.png" alt="" className="size-8 object-contain" />
        <span className="text-xl font-semibold tracking-tight">Hondo.</span>
      </div>
      <div className="flex flex-1 items-center py-16">
        <div className="mx-auto flex w-full max-w-md flex-col gap-8">
          <WorkspaceTile name={team.name} />
          {slackError && (
            <Alert variant="destructive">
              <AlertDescription>
                {installErrorMessage(slackError)}
              </AlertDescription>
            </Alert>
          )}
          {!team.workspace_id ? (
            <NotInstalled team={team} />
          ) : team.slack === "reinstall_required" ? (
            <ReinstallRequired team={team} />
          ) : team.slack === "unavailable" ? (
            <SlackUnavailable team={team} />
          ) : (
            <NoAccess team={team} />
          )}
          <Separator />
          <div className="flex flex-col gap-4">
            <p className="text-sm leading-6 text-muted-foreground">
              Signed in as{" "}
              <span className="font-medium text-foreground">
                {user.display_name || user.email}
              </span>
              {user.display_name && ` (${user.email})`} in {team.name}.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                nativeButton={false}
                render={<a href={loginUrl()} />}
              >
                Sign in with another workspace
              </Button>
              <Button
                variant="ghost"
                disabled={logout.isPending}
                onClick={() => logout.mutate()}
              >
                <RiLogoutBoxLine data-icon="inline-start" />
                Sign out
              </Button>
            </div>
          </div>
        </div>
      </div>
    </main>
  )
}
