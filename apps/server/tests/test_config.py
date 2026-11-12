from __future__ import annotations

import os

import pytest
from pydantic import SecretStr, ValidationError

from hondo.config import (
    ApiConfig,
    Config,
    ConnectorsConfig,
    EngagePolicy,
    LogfireConfig,
    LoggingConfig,
    RuntimeConfig,
    SlackConfig,
    WorkspaceConfig,
)


def make_slack(**overrides: object) -> SlackConfig:
    defaults: dict[str, object] = {
        "app_token": SecretStr("xapp-secret"),
    }
    return SlackConfig(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_slack_config_defaults() -> None:
    config = make_slack()
    assert config.mode == "socket"
    assert config.signing_secret is None


def test_slack_config_requires_the_app_token() -> None:
    with pytest.raises(ValidationError):
        SlackConfig()  # type: ignore[call-arg]


def test_slack_config_masks_tokens() -> None:
    config = make_slack()
    assert "xapp-secret" not in repr(config)
    assert config.app_token.get_secret_value() == "xapp-secret"


def test_slack_config_normalizes_mode() -> None:
    config = make_slack(mode=" Socket ")
    assert config.mode == "socket"


def test_slack_config_http_mode_requires_signing_secret() -> None:
    with pytest.raises(ValidationError, match="signing_secret"):
        make_slack(mode="http")
    config = make_slack(mode="http", signing_secret=SecretStr("sig"))
    assert config.mode == "http"


def test_engage_policy_defaults() -> None:
    policy = EngagePolicy()
    assert policy.respond_to_mentions
    assert policy.respond_to_dms
    assert policy.allows_channel("C_ANY")


def test_engage_policy_allowlist() -> None:
    policy = EngagePolicy(channel_allowlist=frozenset({"C01"}))
    assert policy.allows_channel("C01")
    assert not policy.allows_channel("C02")


def test_runtime_config_bounds() -> None:
    with pytest.raises(ValidationError):
        RuntimeConfig(queue_polling_interval=0)
    config = RuntimeConfig(queue_polling_interval=0.1)
    assert config.system_schema == "hondo_dbos"


def test_logging_config_resolved_format() -> None:
    assert LoggingConfig().resolved_format(is_prod=False) == "text"
    assert LoggingConfig().resolved_format(is_prod=True) == "json"
    assert LoggingConfig(format="text").resolved_format(is_prod=True) == "text"


def test_workspace_config_defaults() -> None:
    config = WorkspaceConfig(slack=make_slack())
    assert config.instructions == "You are a helpful teammate."
    assert config.engage == EngagePolicy()


def test_connectors_refuse_private_networks_by_default() -> None:
    assert not ConnectorsConfig().allow_private_networks


def test_api_config_rejects_weak_session_secret() -> None:
    for weak in ("", "   ", "short", "x" * 31):
        with pytest.raises(ValidationError, match="at least 32 bytes"):
            ApiConfig(session_secret=SecretStr(weak))
    # Whitespace padding must not count toward the minimum.
    with pytest.raises(ValidationError, match="at least 32 bytes"):
        ApiConfig(session_secret=SecretStr("short-secret" + " " * 32))
    assert ApiConfig(session_secret=SecretStr("x" * 32)).session_secret is not None
    assert ApiConfig().session_secret is None  # unset is allowed; create_app enforces presence


def test_api_config_rejects_wildcard_cors_origin() -> None:
    with pytest.raises(ValidationError, match="explicit origins"):
        ApiConfig(cors_origins=("*",))
    with pytest.raises(ValidationError, match="explicit origins"):
        ApiConfig(cors_origins=("https://app.example", "*"))
    assert ApiConfig(cors_origins=("https://app.example",)).cors_origins


def test_api_config_validates_urls_and_derives_cookie_secure() -> None:
    with pytest.raises(ValidationError, match="http"):
        ApiConfig(public_base_url="ftp://example.com")
    with pytest.raises(ValidationError, match="http"):
        ApiConfig(app_url="not-a-url")
    assert not ApiConfig().cookie_secure
    assert ApiConfig(public_base_url="https://hondo.example").cookie_secure
    # Scheme comparison is case-insensitive; the Secure flag must not silently
    # drop on an uppercased-but-valid URL.
    assert ApiConfig(public_base_url="HTTPS://Hondo.Example/").cookie_secure


def test_api_config_app_url_follows_public_base_url() -> None:
    assert ApiConfig(public_base_url="https://hondo.example/").app_url == ("https://hondo.example")
    split = ApiConfig(public_base_url="https://api.example", app_url="https://app.example")
    assert split.app_url == "https://app.example"
    assert ApiConfig().app_url == "http://localhost:5173"


def test_config_reads_prefixed_nested_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HONDO_ENVIRONMENT", " PROD ")
    monkeypatch.setenv("HONDO_WORKSPACE__SLACK__APP_TOKEN", "xapp-env")
    monkeypatch.setenv("HONDO_WORKSPACE__ENGAGE__RESPOND_TO_DMS", "false")
    monkeypatch.setenv("HONDO_WORKSPACE__INSTRUCTIONS", "Be concise.")
    monkeypatch.setenv("HONDO_DATABASE__URL", "postgresql://hondo:hondo@localhost:5455/hondo")
    monkeypatch.setenv("HONDO_CRYPTO__TOKEN_KEY", "crypto-env")
    monkeypatch.setenv("HONDO_API__HOST", "0.0.0.0")
    monkeypatch.setenv("HONDO_API__PORT", "8080")
    monkeypatch.setenv("HONDO_API__SESSION_SECRET", "session-env-0123456789abcdef0123456789")
    monkeypatch.setenv("HONDO_API__PUBLIC_BASE_URL", "https://hondo.example/")
    monkeypatch.setenv("HONDO_AUTH__SLACK__CLIENT_ID", "slack-client-id")
    monkeypatch.setenv("HONDO_AUTH__SLACK__CLIENT_SECRET", "slack-client-secret")
    # Left over from an older .env: unknown settings are ignored, not fatal.
    monkeypatch.setenv("HONDO_AUTH__GOOGLE__CLIENT_ID", "google-client-id")
    monkeypatch.setenv("HONDO_AUTH__SESSION_TTL_DAYS", "7")
    monkeypatch.setenv("HONDO_RUNTIME__RECONCILE_INTERVAL", "8")
    monkeypatch.setenv("HONDO_CONNECTORS__ALLOW_PRIVATE_NETWORKS", "true")

    config = Config(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert config.is_prod
    assert config.workspace.slack.app_token.get_secret_value() == "xapp-env"
    assert not config.workspace.engage.respond_to_dms
    assert config.workspace.instructions == "Be concise."
    assert config.database.url.get_secret_value().endswith("/hondo")
    assert config.crypto is not None
    assert config.crypto.token_key.get_secret_value() == "crypto-env"
    assert config.api.host == "0.0.0.0"
    assert config.api.port == 8080
    assert config.api.session_secret is not None
    assert config.api.session_secret.get_secret_value() == "session-env-0123456789abcdef0123456789"
    assert config.api.public_base_url == "https://hondo.example"  # trailing slash stripped
    assert config.api.app_url == "https://hondo.example"
    assert config.auth.slack is not None
    assert config.auth.slack.client_id == "slack-client-id"
    assert config.auth.slack.client_secret.get_secret_value() == "slack-client-secret"
    assert not hasattr(config.auth, "google")
    assert config.auth.session_ttl_days == 7
    assert config.runtime.reconcile_interval == 8
    assert config.connectors.allow_private_networks
    assert config.logging.resolved_format(is_prod=config.is_prod) == "json"


def test_config_requires_slack_and_database(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in list(os.environ):
        if var.startswith("HONDO_"):
            monkeypatch.delenv(var)
    with pytest.raises(ValidationError):
        Config(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_tracing_is_off_unless_the_operator_turns_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    import logfire

    from hondo import observability

    calls: list[object] = []
    monkeypatch.setattr(logfire, "configure", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(logfire, "instrument_pydantic_ai", lambda **kwargs: None)
    monkeypatch.setenv("LOGFIRE_TOKEN", "pylf_v1_us_test")

    monkeypatch.setattr(observability, "_configured", False)
    observability.configure_observability(LogfireConfig())
    assert calls == []

    monkeypatch.setattr(observability, "_configured", False)
    observability.configure_observability(LogfireConfig(enabled=True))
    assert len(calls) == 1
