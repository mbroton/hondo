import { ApiError } from "@/lib/api/client"
import type { BuiltinConnector } from "@/lib/api/types"

export function serverHost(url: string): string {
  try {
    return new URL(url).host
  } catch {
    return "Invalid URL"
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const messages: Record<string, string> = {
      connection_changed:
        "Another admin changed this connector. Discard your changes to load the latest version before saving.",
      connection_access_changed:
        "Connector access changed. Reload the page and review the account and channel settings.",
      connection_not_found:
        "This connector no longer exists. Return to Connectors to see the current list.",
      connection_name_taken:
        "A connector with this name already exists in this workspace.",
      connection_not_ready:
        "Connect an account and select at least one tool and channel before enabling.",
      invalid_server_url:
        "Enter a public HTTPS server URL. Local and private addresses are not supported.",
      oauth_client_required:
        "This server needs an OAuth client. Open Advanced setup and enter the client details from its provider.",
      oauth_not_supported:
        "This server's sign-in method is not supported. Check its MCP setup instructions, or connect with a key.",
      sign_in_required: "Connect the account again to restore access.",
      tools_changed:
        "The tool list changed. Refresh the tools and review your selection.",
      invalid_tools:
        "The server returned tools that cannot be used. Contact its administrator.",
      connection_unavailable:
        "Hondo could not reach the server. Try again in a few minutes.",
      builtin_not_found:
        "This connector is no longer available. Reload the page and select a connector again.",
      key_refused:
        "The service refused this token. Check the token and its permissions.",
      not_builtin:
        "This connector cannot use the built-in setup. Reload the page and try again.",
      not_http:
        "This connector is an MCP server. Its tools come from the server.",
      tool_url_outside_connector:
        "Use the connector's API address in the URL: the same scheme, host and port.",
      tool_name_taken:
        "A tool with this name already exists in this connector.",
      tool_not_found:
        "This tool no longer exists. Return to the connector to see its tools.",
      too_many_tools: "This connector has the maximum number of tools.",
      invalid_arguments: "Enter a value for each parameter.",
    }
    if (error.status === 401) return "Your session expired. Sign in again."
    if (error.detail && messages[error.detail]) return messages[error.detail]
  }
  return "The connector could not be updated. Try again."
}

export function builtinErrorMessage(error: unknown): string {
  return error instanceof ApiError && error.detail === "invalid_request"
    ? "Check the values that you entered. Use only standard keyboard characters."
    : errorMessage(error)
}

export function keyErrorMessage(
  key: { headers: unknown } | { inputs: unknown },
  error: unknown
): string {
  if ("inputs" in key) return builtinErrorMessage(error)
  return error instanceof ApiError && error.detail === "invalid_request"
    ? "Check the header names and values. Each name must be unique, and Hondo sets protocol headers such as Content-Type itself."
    : errorMessage(error)
}

export type BuiltinValues = Record<string, string>

export function filledInputs(
  builtin: BuiltinConnector,
  values: BuiltinValues
): BuiltinValues | null {
  const inputs = Object.fromEntries(
    builtin.inputs.map((input) => [
      input.name,
      (values[input.name] ?? "").trim(),
    ])
  )
  return Object.values(inputs).every(Boolean) ? inputs : null
}

export function toolErrorMessage(error: unknown): string {
  return error instanceof ApiError && error.detail === "invalid_request"
    ? "Check the request. Put {name} parameters only in the path, the query or the body, describe each one, and enter the body as JSON."
    : errorMessage(error)
}
