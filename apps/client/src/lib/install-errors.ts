// Codes the Slack install callback puts in `?slack_error=`.
const installErrorMessages: Record<string, string> = {
  denied: "The install was cancelled. Start again whenever you're ready.",
  expired: "That install link expired. Start again.",
  exchange_failed: "Slack didn't complete the install. Try again.",
  install_failed: "Hondo couldn't finish the install. Try again.",
  other_workspace:
    "Hondo already works in a different Slack workspace. It can work in only one.",
  not_admin:
    "Hondo is already in this Slack workspace. Ask an admin to give you access.",
  unavailable: "Installing from here isn't available yet.",
}

export function installErrorMessage(code: string): string {
  return installErrorMessages[code] ?? installErrorMessages.install_failed
}
