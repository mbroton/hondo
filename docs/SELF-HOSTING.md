# Self-hosting Hondo

One deployment serves one Slack workspace. You need:

- A server with Docker and Docker Compose.
- A domain name with HTTPS. Slack OAuth needs HTTPS.
- Permission to create and install a Slack app in your workspace.
- A Vercel AI Gateway, OpenAI, or Anthropic API key.

## 1. Create the Slack app

1. In [Slack app settings](https://api.slack.com/apps), select **Create New
   App → From a manifest**, and paste
   [`apps/server/slack-manifest.json`](../apps/server/slack-manifest.json).
2. In **Basic Information → App-Level Tokens**, generate a token with the
   `connections:write` scope. Copy the token (`xapp-…`), the **Client ID**,
   and the **Client Secret**.
3. In **OAuth & Permissions → Redirect URLs**, add these two URLs, with your
   domain:

   ```text
   https://hondo.example.com/api/auth/slack/callback
   https://hondo.example.com/api/slack/install/callback
   ```

## 2. Configure Hondo

```sh
git clone https://github.com/mbroton/hondo.git
cd hondo
cp .env.example .env
```

Edit `.env`. Set your HTTPS address and the Slack values from step 1.
Generate the encryption key and the session secret:

```sh
openssl rand -base64 32 | tr '+/' '-_'   # HONDO_CRYPTO__TOKEN_KEY
openssl rand -base64 48                  # HONDO_API__SESSION_SECRET
```

Keep a backup of the encryption key. Without it, Hondo cannot read the
tokens and keys in its database.

## 3. Start Hondo

```sh
docker compose up -d
```

Docker Compose builds the image from source and starts Postgres, a one-time
database migration, the API, and the Slack worker. The API serves the
dashboard and the API on `127.0.0.1:8000`. To use another address, set
`HONDO_PORT` in `.env`. Until you add Hondo to Slack in step 5, the
worker stops and starts again. This is expected.

## 4. Put HTTPS in front

Send your domain to `127.0.0.1:8000` with a reverse proxy that terminates
HTTPS. For example, with [Caddy](https://caddyserver.com/):

```text
hondo.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

## 5. Add Hondo to Slack

Open your HTTPS address and select **Add to Slack**. The person who adds
Hondo becomes its first admin. Slack workspace owners and admins can also
open the dashboard.

## 6. Select a model

In the dashboard, open **Settings**. Add a connection to Vercel AI Gateway,
OpenAI, or Anthropic with your API key, then select the model. Until you
select a model, Hondo answers in Slack with a setup notice.

## 7. Ask Hondo

Invite the bot to a channel (`/invite @hondo`) and mention it:

```text
@hondo what is blocking ENG-123? Check the ticket and the linked pull requests.
```

You can also send it a direct message. To connect your tools, open
**Connectors** in the dashboard. By default, connectors accept only public
HTTPS servers. For MCP servers in your own network, set
`HONDO_CONNECTORS__ALLOW_PRIVATE_NETWORKS=true` in `.env`. The
[server notes](../apps/server/README.md) tell more about connectors and keys.

## Update

```sh
git pull
docker compose up -d --build
```

Migrations run before the API and the worker start. To use a released image
instead of a build from source, set `HONDO_IMAGE` in `.env`.

## Data and backups

Postgres stores conversations, thread summaries, run state, admin accounts,
and the encrypted Slack tokens, model keys, and connector credentials. The
data is in the Docker volume `pgdata` of the Compose project.

Back up the database:

```sh
docker compose exec -T postgres pg_dump -U hondo -Fc hondo > hondo.dump
```

Restore it into a new deployment, before you start the API and the worker:

```sh
docker compose up -d postgres
docker compose exec -T postgres pg_restore -U hondo -d hondo --clean --if-exists < hondo.dump
docker compose up -d
```

Also back up `HONDO_CRYPTO__TOKEN_KEY`, and keep it apart from the database
backup. The API and the worker must use the same key.

Where data goes:

- The model provider gets the prompts, the conversation context, and the tool
  results.
- Connected MCP servers and HTTP APIs get the tool calls.
- Logfire gets traces only when you set `HONDO_LOGFIRE__ENABLED=true` and a
  `LOGFIRE_TOKEN`.
