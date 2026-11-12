import { Link, useSearchParams } from "react-router"

import { SlackInstallButton } from "@/components/add-to-slack"
import { Button } from "@/components/ui/button"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Separator } from "@/components/ui/separator"
import { useMe } from "@/lib/api/queries"
import { loginUrl } from "@/lib/auth"
import { installErrorMessage } from "@/lib/install-errors"
import { RiSlackFill } from "@remixicon/react"

const errorMessages: Record<string, string> = {
  denied: "Sign-in was cancelled. Try again whenever you're ready.",
  exchange_failed: "We couldn't complete sign-in. Please try again.",
  unverified_email:
    "Your email address isn't verified in Slack. Verify it there, then try again.",
  sign_in_failed: "Something went wrong during sign-in. Please try again.",
  other_workspace:
    "Hondo works in a different Slack workspace. Sign in with an account from that workspace.",
}

const genericErrorMessage =
  "Something went wrong during sign-in. Please try again."

export default function LoginPage() {
  const [searchParams] = useSearchParams()
  const { data: me } = useMe()
  const errorCode = searchParams.get("error")
  const slackError = searchParams.get("slack_error")
  const errorMessage = errorCode
    ? (errorMessages[errorCode] ?? genericErrorMessage)
    : slackError
      ? installErrorMessage(slackError)
      : null

  return (
    <main className="grid min-h-svh lg:grid-cols-2">
      <section className="relative hidden flex-col justify-between border-r bg-sidebar p-12 lg:flex xl:p-16">
        <div className="flex items-center gap-3">
          <img src="/logo-sm.png" alt="" className="size-9 object-contain" />
          <span className="text-2xl font-semibold tracking-tight">Hondo.</span>
        </div>
        <div className="flex max-w-lg flex-col gap-7 py-16">
          <div className="flex size-20 items-center justify-center rounded-2xl bg-primary">
            <img src="/logo-sm.png" alt="" className="size-12 object-contain" />
          </div>
          <p className="text-5xl leading-[1.1] font-semibold tracking-tight xl:text-6xl">
            A helping hand.
            <br />
            Right in Slack.
          </p>
          <p className="max-w-sm text-lg leading-8 text-muted-foreground">
            Give your team an AI agent where the conversation already happens.
          </p>
          <div className="mt-3 flex items-center gap-3 text-sm text-muted-foreground">
            <RiSlackFill className="size-5" aria-hidden="true" />
            <span>Your conversations. Your models. Your workspace.</span>
          </div>
        </div>
        <p className="text-sm text-muted-foreground">
          Less switching. More doing.
        </p>
      </section>
      <section className="flex flex-col items-center justify-center px-6 py-12 sm:px-12">
        <div className="flex w-full max-w-sm flex-col gap-8">
          <div className="flex items-center gap-3 lg:hidden">
            <img src="/logo-sm.png" alt="" className="size-9 object-contain" />
            <span className="text-2xl font-semibold tracking-tight">
              Hondo.
            </span>
          </div>
          <div className="flex flex-col gap-3">
            <h1 className="text-3xl font-semibold tracking-tight">
              Sign in to Hondo
            </h1>
            <p className="text-sm leading-6 text-muted-foreground">
              Use the Slack account of the workspace where Hondo works.
            </p>
          </div>
          {errorMessage && (
            <Alert variant="destructive">
              <AlertDescription>{errorMessage}</AlertDescription>
            </Alert>
          )}
          <Button
            size="lg"
            className="w-full"
            nativeButton={false}
            render={<a href={loginUrl()} />}
          >
            <RiSlackFill data-icon="inline-start" />
            Sign in with Slack
          </Button>
          {me && (
            <Button
              variant="ghost"
              size="lg"
              className="w-full"
              nativeButton={false}
              render={<Link to="/" />}
            >
              Back to {me.team.name || "the dashboard"}
            </Button>
          )}
          <Separator />
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1">
              <p className="text-sm font-medium">New to Hondo?</p>
              <p className="text-sm leading-6 text-muted-foreground">
                Add it to your Slack workspace. You become its admin and set it
                up here.
              </p>
            </div>
            <SlackInstallButton
              variant="outline"
              size="lg"
              className="w-full"
            />
          </div>
        </div>
      </section>
    </main>
  )
}
