"""Initial schema.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TABLES = [
    "workspaces",
    "users",
    "auth_sessions",
    "workspace_admins",
    "provider_connections",
    "agents",
    "mcp_connections",
    "mcp_oauth_attempts",
    "mcp_calls",
    "mcp_audit_events",
    "skills",
    "events",
    "sessions",
    "session_messages",
    "steer_mail",
    "run_snapshots",
    "run_notices",
    "tool_approvals",
    "usage_events",
    "thread_summaries",
]

SCHEMA = """
CREATE TABLE workspaces (
    id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slack_team_id         text NOT NULL UNIQUE,
    name                  text NOT NULL,
    status                text NOT NULL DEFAULT 'active',
    bot_token_encrypted   text NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now(),
    mention_only_channels text[] NOT NULL DEFAULT '{}',
    listening_channels    jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE users (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email           text NOT NULL,
    display_name    text NOT NULL DEFAULT '',
    avatar_url      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    slack_team_id   text NOT NULL,
    slack_user_id   text NOT NULL,
    slack_team_name text NOT NULL DEFAULT '',
    CONSTRAINT users_slack_account_key UNIQUE (slack_team_id, slack_user_id)
);

CREATE TABLE auth_sessions (
    token_hash text PRIMARY KEY,
    user_id    uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

CREATE INDEX auth_sessions_user_idx ON auth_sessions (user_id);

CREATE TABLE workspace_admins (
    workspace_id  uuid NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    slack_user_id text NOT NULL,
    added_by      uuid REFERENCES users (id) ON DELETE SET NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, slack_user_id)
);

CREATE TABLE provider_connections (
    provider          text NOT NULL,
    api_key_encrypted text NOT NULL,
    check_status      text NOT NULL CHECK (check_status IN ('valid', 'rejected')),
    checked_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name              text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    workspace_id      uuid NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    UNIQUE (workspace_id, id)
);

CREATE UNIQUE INDEX provider_connections_workspace_name_key
    ON provider_connections (workspace_id, lower(name));

CREATE TABLE agents (
    id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id           uuid NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    name                   text NOT NULL DEFAULT 'hondo',
    provider_connection_id uuid,
    model_name             text CHECK (model_name IS NULL OR char_length(model_name) BETWEEN 1 AND 200),
    updated_at             timestamptz DEFAULT now(),
    reads_images           boolean NOT NULL DEFAULT true,
    UNIQUE (workspace_id, name),
    CONSTRAINT agents_check CHECK ((provider_connection_id IS NULL) = (model_name IS NULL)),
    CONSTRAINT agents_provider_connection_fkey FOREIGN KEY (workspace_id, provider_connection_id)
        REFERENCES provider_connections (workspace_id, id)
);

CREATE TABLE mcp_connections (
    id                 uuid PRIMARY KEY,
    workspace_id       uuid NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    name               text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 100),
    url                text NOT NULL,
    authorized_by      uuid REFERENCES users (id) ON DELETE SET NULL,
    enabled            boolean NOT NULL DEFAULT false,
    all_channels       boolean NOT NULL DEFAULT false,
    channels           text[] NOT NULL DEFAULT '{}',
    tools              jsonb NOT NULL DEFAULT '[]'::jsonb,
    allowed_tools      jsonb NOT NULL DEFAULT '{}'::jsonb,
    status             text NOT NULL DEFAULT 'sign_in_required',
    revision           bigint NOT NULL DEFAULT 1,
    credential_version bigint NOT NULL DEFAULT 0,
    credentials        text,
    refresh_claim      uuid,
    refresh_until      timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    review_required    boolean NOT NULL DEFAULT false,
    auth               text CHECK (auth IN ('oauth', 'key')),
    header_names       text[] NOT NULL DEFAULT '{}',
    builtin            text,
    ask_tools          text[] NOT NULL DEFAULT '{}',
    kind               text NOT NULL DEFAULT 'mcp' CHECK (kind IN ('mcp', 'http')),
    UNIQUE (workspace_id, name),
    CONSTRAINT mcp_connections_auth_credentials CHECK ((auth IS NULL) = (credentials IS NULL))
);

CREATE TABLE mcp_oauth_attempts (
    state_hash         text PRIMARY KEY,
    connection_id      uuid NOT NULL UNIQUE REFERENCES mcp_connections (id) ON DELETE CASCADE,
    user_id            uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    session_hash       text NOT NULL,
    credential_version bigint NOT NULL,
    payload            text NOT NULL,
    expires_at         timestamptz NOT NULL DEFAULT now() + interval '10 minutes'
);

CREATE TABLE mcp_calls (
    id               text PRIMARY KEY,
    connection_id    uuid NOT NULL REFERENCES mcp_connections (id) ON DELETE CASCADE,
    run_id           text NOT NULL,
    tool_name        text NOT NULL,
    args_hash        text NOT NULL,
    channel          text NOT NULL,
    requested_by     text,
    status           text NOT NULL DEFAULT 'started',
    result_encrypted text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    read_only        boolean NOT NULL DEFAULT false
);

CREATE INDEX mcp_calls_connection_run ON mcp_calls (connection_id, run_id, status);
CREATE INDEX mcp_calls_run ON mcp_calls (run_id);

CREATE TABLE mcp_audit_events (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    workspace_id  uuid NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    connection_id uuid REFERENCES mcp_connections (id) ON DELETE SET NULL,
    actor_id      uuid REFERENCES users (id) ON DELETE SET NULL,
    action        text NOT NULL,
    revision      bigint NOT NULL,
    details       jsonb NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE skills (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id   uuid NOT NULL REFERENCES workspaces (id) ON DELETE CASCADE,
    name           text NOT NULL CHECK (char_length(name) BETWEEN 1 AND 64),
    description    text NOT NULL CHECK (char_length(description) BETWEEN 1 AND 1024),
    content        text NOT NULL CHECK (char_length(content) BETWEEN 1 AND 20000),
    published      boolean NOT NULL DEFAULT false,
    all_channels   boolean NOT NULL DEFAULT false,
    channels       text[] NOT NULL DEFAULT '{}',
    source_channel text,
    source_ts      text,
    draft_token    text,
    created_by     uuid REFERENCES users (id) ON DELETE SET NULL,
    updated_by     uuid REFERENCES users (id) ON DELETE SET NULL,
    revision       bigint NOT NULL DEFAULT 1,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT skills_workspace_name_key UNIQUE (workspace_id, name),
    CONSTRAINT skills_source_thread_key UNIQUE (workspace_id, source_channel, source_ts),
    CONSTRAINT skills_source_thread_check CHECK ((source_channel IS NULL) = (source_ts IS NULL))
);

CREATE TABLE events (
    event_id     text PRIMARY KEY,
    kind         text NOT NULL,
    team         text NOT NULL,
    channel      text NOT NULL,
    ts           text NOT NULL,
    user_id      text,
    thread_ts    text,
    text         text NOT NULL DEFAULT '',
    is_dm        boolean NOT NULL DEFAULT false,
    mentions_bot boolean NOT NULL DEFAULT false,
    payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    received_at  timestamptz NOT NULL DEFAULT now(),
    files        jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE sessions (
    team          text NOT NULL,
    channel       text NOT NULL,
    thread_root   text NOT NULL,
    state         text NOT NULL DEFAULT 'idle',
    last_event_id text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    roster        jsonb NOT NULL DEFAULT '{}'::jsonb,
    context       jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (team, channel, thread_root)
);

CREATE INDEX sessions_team_updated_idx ON sessions (team, updated_at DESC);

CREATE TABLE session_messages (
    id              bigserial PRIMARY KEY,
    team            text NOT NULL,
    channel         text NOT NULL,
    thread_root     text NOT NULL,
    run_id          text NOT NULL,
    seq             integer NOT NULL,
    history_version integer NOT NULL,
    message         jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (run_id, seq)
);

CREATE INDEX session_messages_session_idx
    ON session_messages (team, channel, thread_root, id);

CREATE TABLE steer_mail (
    id                 bigserial PRIMARY KEY,
    team               text NOT NULL,
    channel            text NOT NULL,
    thread_root        text NOT NULL,
    event_id           text NOT NULL UNIQUE,
    user_id            text,
    text               text NOT NULL DEFAULT '',
    ts                 text NOT NULL,
    priority           text NOT NULL DEFAULT 'asap',
    target_workflow_id text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    consumed_at        timestamptz,
    claimed_by         text,
    files              jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX steer_mail_unconsumed_idx
    ON steer_mail (team, channel, thread_root, id) WHERE consumed_at IS NULL;
CREATE INDEX steer_mail_stale_idx ON steer_mail (created_at) WHERE consumed_at IS NULL;
CREATE INDEX steer_mail_claimed_by_idx ON steer_mail (claimed_by) WHERE claimed_by IS NOT NULL;

CREATE TABLE run_snapshots (
    workflow_id     text PRIMARY KEY,
    team            text NOT NULL,
    channel         text NOT NULL,
    thread_root     text NOT NULL,
    history_version integer NOT NULL,
    payload         jsonb NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX run_snapshots_created_at_idx ON run_snapshots (created_at);

CREATE TABLE run_notices (
    workflow_id text PRIMARY KEY,
    text        text NOT NULL DEFAULT '',
    attempts    integer NOT NULL DEFAULT 0,
    created_at  timestamptz NOT NULL DEFAULT now(),
    posted_at   timestamptz
);

CREATE TABLE tool_approvals (
    id          text PRIMARY KEY,
    workflow_id text NOT NULL,
    team        text NOT NULL,
    channel     text NOT NULL,
    message_ts  text,
    status      text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    decided_by  text,
    decided_at  timestamptz,
    expires_at  timestamptz NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE usage_events (
    id           bigserial PRIMARY KEY,
    team         text NOT NULL,
    kind         text NOT NULL,
    provider     text NOT NULL,
    resource     text NOT NULL,
    quantity_in  bigint NOT NULL DEFAULT 0,
    quantity_out bigint NOT NULL DEFAULT 0,
    metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    channel      text,
    thread_root  text,
    workflow_id  text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, kind, resource)
);

CREATE INDEX usage_events_team_created_at_idx ON usage_events (team, created_at);

CREATE TABLE thread_summaries (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    team       text NOT NULL,
    channel    text NOT NULL,
    thread_ts  text NOT NULL,
    private    boolean NOT NULL,
    text       text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    search     tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    covered_ts text NOT NULL,
    UNIQUE (team, channel, thread_ts)
);

CREATE INDEX thread_summaries_search_idx ON thread_summaries USING gin (search);
"""


def upgrade() -> None:
    op.get_bind().exec_driver_sql(SCHEMA)


def downgrade() -> None:
    op.get_bind().exec_driver_sql(f"DROP TABLE {', '.join(reversed(TABLES))}")
