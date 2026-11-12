from __future__ import annotations

from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["dev", "prod"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["json", "text"]
SlackMode = Literal["socket", "http"]


class SlackConfig(BaseModel):
    # No bot token here: it is per-workspace, granted by the install flow, and
    # read encrypted from the workspaces table. The app-level token below
    # belongs to the Slack app itself, so it stays env-only.
    app_token: SecretStr = Field(
        min_length=1,
        description="Slack app-level token (xapp-...) used for the Socket Mode connection.",
    )
    signing_secret: SecretStr | None = Field(
        default=None,
        description=(
            "Slack request signing secret. Required in http mode; unused in socket "
            "mode, which does not verify request signatures."
        ),
    )
    mode: SlackMode = Field(
        default="socket",
        description="Ingress mode: socket connects out via Socket Mode; http serves the Events API.",
    )

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, value: str) -> str:
        return value.strip().lower()

    @model_validator(mode="after")
    def validate_http_mode_settings(self) -> Self:
        if self.mode == "http" and (
            self.signing_secret is None or not self.signing_secret.get_secret_value()
        ):
            raise ValueError("slack.signing_secret must be set when slack.mode is http")
        return self


class EngagePolicy(BaseModel):
    respond_to_mentions: bool = Field(
        default=True,
        description="Engage when the bot is @-mentioned in a channel.",
    )
    respond_to_dms: bool = Field(
        default=True,
        description="Engage on direct messages to the bot.",
    )
    channel_allowlist: frozenset[str] | None = Field(
        default=None,
        description="Channel IDs the bot may engage in. None allows every channel.",
    )

    def allows_channel(self, channel: str) -> bool:
        return self.channel_allowlist is None or channel in self.channel_allowlist


class WorkspaceConfig(BaseModel):
    """Maps 1:1 to a future per-workspace database row; env-loaded until then."""

    slack: SlackConfig
    engage: EngagePolicy = Field(default_factory=EngagePolicy)
    instructions: str = Field(
        default="You are a helpful teammate.",
        min_length=1,
        max_length=10_000,
        description="The teammate's persona/instructions prompt.",
    )


class ApiConfig(BaseModel):
    host: str = Field(
        default="0.0.0.0",
        min_length=1,
        description="Host interface for the control-plane API server.",
    )
    port: int = Field(
        default=8000,
        ge=1,
        le=65535,
        description="TCP port for the control-plane API server.",
    )
    public_base_url: str = Field(
        default="http://localhost:5173",
        min_length=1,
        description=(
            "Base URL the browser reaches the API on; OAuth redirect URIs are "
            "built from it. Defaults to the Vite dev server, which proxies /api "
            "so the session cookie stays first-party."
        ),
    )
    app_url: str = Field(
        default="http://localhost:5173",
        min_length=1,
        description=(
            "Base URL of the SPA; sign-in redirects land on it. Defaults to "
            "public_base_url when that is set."
        ),
    )
    dashboard_dir: Path | None = Field(
        default=None,
        description=(
            "Directory with the built dashboard. When set, the API serves it on / "
            "and answers client routes with its index.html."
        ),
    )
    cors_origins: tuple[str, ...] = Field(
        default=(),
        description=(
            "Origins allowed to call the API with credentials. Empty by default: "
            "dev traffic flows through the Vite proxy and prod serves the SPA and "
            "API from one origin, so CORS is normally unused."
        ),
    )
    session_secret: SecretStr | None = Field(
        default=None,
        description=(
            "Secret signing the short-lived OAuth state/nonce cookie. Required "
            "to run the API; login sessions themselves are database-backed."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def default_app_url(cls, data: object) -> object:
        if isinstance(data, dict) and "public_base_url" in data and "app_url" not in data:
            return {**data, "app_url": data["public_base_url"]}
        return data

    @field_validator("public_base_url", "app_url", mode="before")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("public_base_url", "app_url")
    @classmethod
    def require_http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
            raise ValueError(f"{value!r} is not an absolute http(s) URL")
        return value

    @field_validator("session_secret")
    @classmethod
    def require_strong_session_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        secret = value.get_secret_value().strip()
        if len(secret.encode()) < 32:
            raise ValueError(
                "api.session_secret must be at least 32 bytes; generate one with: "
                'python -c "import secrets; print(secrets.token_urlsafe(48))"'
            )
        return value

    @field_validator("cors_origins")
    @classmethod
    def reject_wildcard_origin(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if "*" in value:
            raise ValueError(
                "api.cors_origins must list explicit origins, not '*': the API "
                "sends credentialed responses, which forbid a wildcard"
            )
        return value

    @property
    def cookie_secure(self) -> bool:
        """Whether browser cookies must be Secure: exactly when users reach us over https."""
        return urlsplit(self.public_base_url).scheme.lower() == "https"


class OIDCClientConfig(BaseModel):
    client_id: str = Field(
        min_length=1,
        description="OAuth client ID from the provider's app registration.",
    )
    client_secret: SecretStr = Field(
        min_length=1,
        description="OAuth client secret paired with the client ID.",
    )


class AuthConfig(BaseModel):
    slack: OIDCClientConfig | None = Field(
        default=None,
        description=(
            "Slack app credentials for sign-in and installation. Unset disables both flows."
        ),
    )
    session_ttl_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="Login session lifetime; applies to the database row and the cookie.",
    )


class RuntimeConfig(BaseModel):
    """Engine knobs for serve(): the DBOS session queue, steering, progress."""

    queue_polling_interval: float = Field(
        default=1.0,
        gt=0,
        le=60,
        description=(
            "Seconds between DBOS queue dequeue polls — the latency floor for "
            "starting a fresh run. Steers are delivered notify-fast regardless."
        ),
    )
    reconcile_interval: float = Field(
        default=5.0,
        gt=0,
        le=300,
        description=(
            "Seconds between stranded-steer sweeps, and the minimum age of "
            "unconsumed steer mail before the sweep re-dispatches it."
        ),
    )
    max_recovery_attempts: int = Field(
        default=5,
        ge=1,
        le=100,
        description=(
            "Crash-recovery attempts before a run is dead-lettered "
            "(DBOS status MAX_RECOVERY_ATTEMPTS_EXCEEDED; resumable by hand)."
        ),
    )
    system_schema: str = Field(
        default="hondo_dbos",
        min_length=1,
        max_length=63,
        description="Postgres schema for the DBOS system tables (self-contained, self-migrating).",
    )
    application_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description=(
            "DBOS application version tag: in-flight runs recover only on a matching "
            "version. Defaults to a source hash; pin it to the Hondo release so "
            "innocuous code churn doesn't strand runs across deploys."
        ),
    )
    backfill_limit: int = Field(
        default=50,
        ge=1,
        le=1000,
        description="Maximum thread messages backfilled into a fresh session's context.",
    )
    debounce_period: float = Field(
        default=3.0,
        ge=0,
        le=60,
        description=(
            "Seconds with no new message in a thread before a run starts, so a burst "
            "of messages gets one answer. Not applied before Hondo's first reply in "
            "a thread, where a fast answer matters more."
        ),
    )
    debounce_max_wait: float = Field(
        default=15.0,
        ge=0,
        le=300,
        description="Most seconds a run waits for quiet, counted from the first waiting message.",
    )
    max_followup_runs: int = Field(
        default=3,
        ge=0,
        le=20,
        description=(
            "Bound on back-to-back follow-up runs draining new steer mail at run "
            "end. Mail left in the ledger at the cap stays unconsumed and reaches "
            "a fresh run via the reconciler; nothing is dropped. Steers a run "
            "already claimed but didn't deliver always get a follow-up, even at "
            "the cap — the cap limits new intake, not delivery of accepted mail."
        ),
    )
    max_history_messages: int = Field(
        default=200,
        ge=2,
        le=10_000,
        description=(
            "Hard bound on session history loaded into a run, in model messages. "
            "Older turns beyond it are dropped at a user-turn boundary — "
            "long-lived sessions (DMs especially) otherwise grow without limit "
            "in context and cost."
        ),
    )
    run_timeout: float = Field(
        default=600.0,
        gt=0,
        description=(
            "Hard ceiling on one session run, in seconds. A run that exceeds it is "
            "stopped and reported in-thread instead of silently occupying the session."
        ),
    )
    stall_timeout: float = Field(
        default=180.0,
        gt=0,
        description=(
            "Watchdog for silent hangs, in seconds. Until a run posts its first "
            "message, this long without a model response or tool result stops it "
            "with an in-thread 'not responding' notice instead of waiting out "
            "run_timeout. Capped at run_timeout."
        ),
    )
    snapshot_max_age_seconds: float = Field(
        default=14 * 24 * 60 * 60,
        gt=0,
        description="Maximum age of unconsumed failed-run snapshots before maintenance GC.",
    )


class LoggingConfig(BaseModel):
    level: LogLevel = Field(
        default="INFO",
        description="Application log level.",
    )
    format: LogFormat | None = Field(
        default=None,
        description="Log output format. Defaults to text in dev and json in prod.",
    )

    def resolved_format(self, *, is_prod: bool) -> LogFormat:
        if self.format is not None:
            return self.format
        return "json" if is_prod else "text"


class LogfireConfig(BaseModel):
    enabled: bool = Field(
        default=False,
        description=(
            "Whether Logfire tracing is configured. Off by default, so nothing is "
            "exported. When on, spans are exported only when a token is reachable "
            "(LOGFIRE_TOKEN or CLI auth)."
        ),
    )
    token: SecretStr | None = Field(
        default=None,
        description="Logfire write token. Optional; the LOGFIRE_TOKEN env var also works.",
    )
    service_name: str = Field(
        default="hondo",
        min_length=1,
        max_length=200,
        description="Logical service identifier attached to every span.",
    )
    console: bool = Field(
        default=False,
        description="Whether Logfire prints spans to the console. Enabling it duplicates logs.",
    )


class DatabaseConfig(BaseModel):
    url: SecretStr = Field(
        min_length=1,
        description="PostgreSQL connection URL (postgresql://user:pass@host:port/db).",
    )


class CryptoConfig(BaseModel):
    token_key: SecretStr = Field(
        min_length=1,
        description="Fernet key used to encrypt Slack bot tokens and provider API keys at rest.",
    )


class ConnectorsConfig(BaseModel):
    allow_private_networks: bool = Field(
        default=False,
        description=(
            "Let MCP servers, and their OAuth endpoints, use private addresses and "
            "plain HTTP. For self-hosted installs with MCP servers inside their own "
            "network. Admins can then reach every address that the server can reach."
        ),
    )


class Config(BaseSettings):
    """Environment-loaded configuration for the deployed teammate.

    Read from HONDO_-prefixed environment variables and .env, with `__` as the
    section delimiter (HONDO_WORKSPACE__SLACK__APP_TOKEN, HONDO_DATABASE__URL, ...).
    Startup loads these settings explicitly; imports do not read configuration.
    """

    model_config = SettingsConfigDict(
        env_prefix="HONDO_",
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    # Env names changed without aliases:
    # HONDO_SLACK__* -> HONDO_WORKSPACE__SLACK__*
    # HONDO_ENGAGE__* -> HONDO_WORKSPACE__ENGAGE__*
    # HONDO_AGENT__INSTRUCTIONS -> HONDO_WORKSPACE__INSTRUCTIONS
    environment: Environment = Field(
        default="dev",
        description="Deployment environment.",
    )
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    logfire: LogfireConfig = Field(default_factory=LogfireConfig)
    database: DatabaseConfig
    crypto: CryptoConfig | None = None
    api: ApiConfig = Field(default_factory=ApiConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    connectors: ConnectorsConfig = Field(default_factory=ConnectorsConfig)
    workspace: WorkspaceConfig

    @property
    def is_prod(self) -> bool:
        return self.environment == "prod"

    @field_validator("environment", mode="before")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        return value.strip().lower()


def load_config() -> Config:
    return Config()  # pyright: ignore[reportCallIssue]
