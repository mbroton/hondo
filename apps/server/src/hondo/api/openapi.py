"""Print the API's OpenAPI schema, the contract the client's types are generated from.

Run with `make openapi`. The app is built with placeholder secrets: the schema
does not depend on configuration, and this app never serves a request.
"""

from __future__ import annotations

import json
from typing import Any

from hondo.api.app import create_app
from hondo.config import Config


def openapi_schema() -> dict[str, Any]:
    config = Config.model_validate(
        {
            "database": {"url": "postgresql://unused"},
            "api": {"session_secret": "x" * 32},
            "crypto": {"token_key": "A" * 43 + "="},
            "workspace": {"slack": {"app_token": "xapp-unused"}},
        }
    )
    return create_app(config).openapi()


def render() -> str:
    return json.dumps(openapi_schema(), indent=2) + "\n"


if __name__ == "__main__":
    print(render(), end="")
