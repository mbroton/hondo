from __future__ import annotations

from pathlib import Path

from hondo.api.openapi import render

CLIENT_SCHEMA = Path(__file__).parents[2] / "client" / "src" / "lib" / "api" / "openapi.json"


def test_client_schema_matches_the_api() -> None:
    assert CLIENT_SCHEMA.read_text() == render(), (
        "API changed: run `make api-types` at the repo root"
    )
