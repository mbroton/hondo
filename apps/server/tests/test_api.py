from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from hondo.api import store as store_module
from hondo.api.app import create_app
from hondo.api.store import ControlStore
from hondo.config import Config
from hondo.crypto import generate_key


def _config(pg_dsn: str) -> Config:
    return Config.model_validate(
        {
            "database": {"url": pg_dsn},
            "api": {"session_secret": "test-session-secret-0123456789abcdef"},
            "crypto": {"token_key": generate_key()},
            "workspace": {"slack": {"app_token": "xapp-test"}},
        }
    )


async def test_healthz(pg_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # The fixture database is already migrated, so spy on the migration call —
    # otherwise this test stays green even if API startup stops migrating.
    migrated: list[str] = []
    monkeypatch.setattr(store_module, "upgrade_to_head", migrated.append)

    store = ControlStore(pg_dsn)
    app = create_app(_config(pg_dsn), store=store)
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert migrated == [pg_dsn]
    with pytest.raises(RuntimeError):
        await store.ping()  # lifespan exit must close the injected store


async def test_healthz_reports_database_failure(pg_dsn: str) -> None:
    class FailingStore(ControlStore):
        async def ping(self) -> None:
            raise ConnectionError("database down")

    app = create_app(_config(pg_dsn), store=FailingStore(pg_dsn))
    transport = ASGITransport(app=app)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/healthz")

    assert response.status_code == 503


async def test_serves_dashboard_with_client_routes(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<div id=root></div>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    config = _config("postgresql://unused@localhost/unused")
    config.api.dashboard_dir = tmp_path
    transport = ASGITransport(app=create_app(config))

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        client_route = await client.get("/workspaces/abc/settings")
        asset = await client.get("/assets/app.js")
        unknown_api = await client.get("/api/no-such-route")

    assert root.status_code == 200
    assert root.text == "<div id=root></div>"
    assert client_route.status_code == 200
    assert client_route.text == "<div id=root></div>"
    assert asset.status_code == 200
    assert asset.text == "console.log(1)"
    assert unknown_api.status_code == 404
    assert unknown_api.json() == {"detail": "Not Found"}
    assert root.headers["cache-control"] == "no-cache"
    assert client_route.headers["cache-control"] == "no-cache"
    assert "cache-control" not in asset.headers
