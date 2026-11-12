"""Command-line entrypoint for the Hondo Slack teammate.

`hondo serve` (or `python -m hondo`) runs the Slack teammate, `hondo api` runs
the control-plane API, and `hondo migrate` applies database migrations. All
commands read `Config` from HONDO_* env vars / .env. Workspaces are registered
from the dashboard's "Add to Slack" flow, not from the command line. This module
is also the Docker entrypoint.
"""

from __future__ import annotations

import argparse
import asyncio

from dotenv import load_dotenv

from hondo.api.serve import serve_api
from hondo.config import load_config
from hondo.deployment import serve_teammate
from hondo.logging import configure_logging
from hondo.mcp.network import allow_private_networks
from hondo.observability import configure_observability
from hondo.store.migrate import upgrade_to_head


def main() -> None:
    parser = argparse.ArgumentParser(prog="hondo", description="AI Slack teammate.")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("serve", help="run the Slack teammate (default)")
    subcommands.add_parser("api", help="run the control-plane API")
    subcommands.add_parser("migrate", help="apply database migrations and exit")
    args = parser.parse_args()

    # Config reads .env itself; load_dotenv covers vars read straight from the
    # environment by other libraries, such as LOGFIRE_TOKEN.
    load_dotenv()
    config = load_config()
    configure_logging(config.logging, is_prod=config.is_prod)
    allow_private_networks(config.connectors.allow_private_networks)

    if args.command == "migrate":
        upgrade_to_head(config.database.url.get_secret_value())
        return

    if args.command == "api":
        configure_observability(config.logfire)
        serve_api(config)
        return

    configure_observability(config.logfire)
    asyncio.run(serve_teammate(config))


if __name__ == "__main__":
    main()
