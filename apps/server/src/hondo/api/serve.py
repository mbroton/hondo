from __future__ import annotations

import uvicorn

from hondo.api.app import create_app
from hondo.config import Config


def serve_api(config: Config) -> None:
    app = create_app(config)
    uvicorn.run(
        app,
        host=config.api.host,
        port=config.api.port,
        log_config=None,
    )
