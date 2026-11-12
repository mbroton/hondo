# Hondo server — operator notes

Setup that [docs/SELF-HOSTING.md](../../docs/SELF-HOSTING.md) does not cover.

## Dashboard access

The dashboard signs people in with Slack. A user is one Slack account.

Only admins open the dashboard. The person who installs Hondo first is its
admin, and Slack workspace owners and admins have access too. Other members
of the Slack workspace use Hondo in Slack only.

Slack requires HTTPS redirect URLs, also for development. Any HTTPS tunnel to
your machine works, for example Tailscale Serve: Slack never connects to your
machine. It sends the browser to the callback, and the code exchange is
outbound.

## External tools

Admins connect remote MCP servers on the **Connectors** page of a
workspace: public HTTPS URL → OAuth sign-in or a key → select tools and
channels → enable.

Server requirements:

- Streamable HTTP, with OAuth and PKCE or a key in request headers.
- For OAuth: published MCP resource metadata and authorization-server metadata.
- For OAuth: client metadata documents, dynamic registration, or a registered
  client entered under **Advanced setup**. For a registered client, the
  redirect URI is `<HONDO_API__PUBLIC_BASE_URL>/api/mcp/oauth/callback`. The
  client metadata document is at `/api/mcp/client-metadata` and must be
  reachable over public HTTPS.
- A public HTTPS address. Redirects and compressed responses are rejected.
  Responses are limited to 2 MB, and calls have time limits.
- By default, private addresses and plain HTTP are rejected. This applies to
  the MCP server and to its OAuth endpoints. For MCP servers in your own
  network, set `HONDO_CONNECTORS__ALLOW_PRIVATE_NETWORKS=true`. Then admins
  can connect to all addresses that the Hondo server can reach, except
  link-local and cloud metadata addresses.

Admins can also add an **HTTP API** connector: an API URL, request headers
(stored like a key), and one tool for each request. A tool has a method, a URL
and an optional JSON body with `{name}` parameters inside JSON strings, and a description of each
parameter. Hondo fills only the parameter values, encoded for their place,
so a value cannot change the host or the path. Each tool URL must use the
host of the connector. The address rules above apply. **Ask first** is on by
default, and a changed tool must be allowed again. The tool page sends one
test request with sample values.

What admins must know:

- Each connection uses one shared external account. Everyone who can steer the
  agent in an allowed channel acts through that account.
- A key is a set of request headers, for example `Authorization: Bearer …`.
  Use a key from a service account, not a personal key, so the connector keeps
  working when a person leaves. Hondo encrypts the header values and never
  shows them again; to change a value, enter the whole key again.
- A selected tool with **Ask first** runs only after a person approves the
  call. Hondo posts the call in the thread with Approve and Reject buttons,
  and any member of the channel can click. With no click in 30 minutes, the
  call does not run. **Ask first** is on by default, unless the server marks
  the tool read-only. A tool without it runs without approval, also if it
  changes data. The tool list does not limit which records a search tool can
  read.
- Direct messages have no connector access.
- Disconnecting stops further calls. It cannot undo a sent action or remove
  data already in the conversation.
- If a call is interrupted before its result is saved, it is not sent again.
  It blocks that connection for the run, and a person must check the external
  service.
- The channel picker and access checks need the Slack `channels:read` and
  `groups:read` scopes. The Approve and Reject buttons need
  **Interactivity** on in the Slack app. After you update the app from
  `slack-manifest.json`, use **Reinstall in Slack** for existing workspaces.

## Keys

`HONDO_CRYPTO__TOKEN_KEY` encrypts Slack tokens, provider keys and connector
credentials. The API and the runtime must use the same key. If you lose it,
generate a new one, install Hondo in Slack again, and enter all provider keys and
connector sign-ins again. There is no command to rotate the key: keep the old
key until all stored values are encrypted again.
