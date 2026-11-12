from __future__ import annotations

import structlog
from dbos import DBOS

from hondo.mcp.store import MCPStore

logger = structlog.get_logger("hondo.mcp")


async def prune_call_journals(store: MCPStore, after: str) -> str:
    run_ids = await store.call_run_ids(after)
    if not run_ids:
        return ""
    # Cancelled workflows can resume; their uncertain actions still need the journal.
    finished = await DBOS.list_workflows_async(
        workflow_ids=run_ids,
        status=["SUCCESS", "ERROR"],
        load_input=False,
        load_output=False,
    )
    if finished:
        deleted = await store.delete_run_calls([run.workflow_id for run in finished])
        logger.info("finished mcp call journals deleted", count=deleted)
    return run_ids[-1]
