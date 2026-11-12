# Contributing to Hondo

The most useful feedback starts with a real task: what you asked, what Hondo
did, and what you needed instead. Open an issue and tell us about it.

Documentation fixes, new built-in connectors, and pull requests are welcome. For a
larger change, open an issue first, so that we can agree on the approach.

## Development setup

You need Python 3.13 and [uv](https://docs.astral.sh/uv/), Node.js 24 with
npm, and Docker.

```text
apps/
|-- server/   Python agent, Slack worker, FastAPI API, and migrations
|-- client/   React dashboard (Vite, TypeScript, Tailwind CSS, shadcn/ui)
`-- website/  Static website (Astro)
```

```sh
cp .env.example .env
uv sync --project apps/server --frozen
npm ci --prefix apps/client
make db-up    # Postgres for development, on 127.0.0.1:5455
make api      # API, in one terminal
make worker   # Slack worker, in a second terminal
make client   # Dashboard dev server on port 5173, in a third terminal
```

Use a separate Slack app for development. Set `HONDO_API__PUBLIC_BASE_URL`
to an HTTPS tunnel to port 5173, and add that address to the redirect URLs of
the app. If the tunnel hostname does not end in `.ts.net`, add it to
`server.allowedHosts` in [`vite.config.ts`](apps/client/vite.config.ts).

```sh
make check    # Lint, format, and type checks, and the client build
make test     # Server tests and client browser tests
```

Tests need Docker: they start their own Postgres container. Before the first
browser test run, run `npx playwright install chromium` in `apps/client`.
Run `make` to see all commands.

## Pull requests

- Keep each change focused on one thing.
- Run `make check` before you open a pull request. If you change code, also
  run `make test`. See [Development setup](#development-setup).
- Use [Conventional Commits](https://www.conventionalcommits.org/), for
  example `fix: handle a missing Slack thread` or `docs: clarify local setup`.
- By contributing, you agree that your contribution is licensed under the
  [Apache License 2.0](LICENSE).

## Security issues

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
