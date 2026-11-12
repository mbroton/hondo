"""Usage accounting: turn a run's model responses into usage_events rows.

The table is generic on purpose: adding a second counted kind (say
"web_search" with searches in quantity_in) is just new rows with a new
(kind, provider, resource) combination here and a caller that emits them —
no migration.

Prices live only in this file, never in the database. A stored price is a
fact about the past that silently goes stale; computing cost at query time
from the map below means a price correction fixes history too. An unknown
resource still gets its tokens recorded — cost is simply None until the map
learns about it.

rate_usage_rows is THE one code path from rows to money. Display (the usage
API endpoint, logs) and enforcement (quotas) must both rate rows through it —
never through separate code, or the number a user sees and the number that
cuts them off will drift apart.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse

from hondo.models import SessionKey

KIND_MODEL_REQUEST = "model_request"


@dataclass(frozen=True)
class ModelPrice:
    """USD per million tokens, maintained by hand against provider price lists.

    Cache rates of None mean "billed at the input rate" — the right default
    for providers whose price lists don't discount cached tokens.
    """

    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cache_read_usd_per_mtok: Decimal | None = None
    cache_write_usd_per_mtok: Decimal | None = None


# Keyed by (kind, provider, resource): the same model can cost differently
# per provider. Base rates only — xAI doubles rates at >=200K prompt tokens;
# accepted approximation for spend visibility. Gateway providers keep the
# gateway's own vendor-qualified model id in the resource ("xai/grok-4.5" is
# Vercel's model id, not a prefix to strip). Only what production actually
# routes is priced; anything else stays truthfully unpriced (cost None).
PRICES: dict[tuple[str, str, str], ModelPrice] = {
    # xAI list price July 2026; the Vercel AI Gateway is zero-markup
    # passthrough. xAI has no cache-write premium, so write stays None.
    (KIND_MODEL_REQUEST, "vercel", "xai/grok-4.5"): ModelPrice(
        Decimal("2"), Decimal("6"), Decimal("0.50")
    ),
}

_MTOK = Decimal(1_000_000)

_VERSION_SUFFIX = re.compile(r"-(?:\d{8}|latest)$")


def _resource_candidates(resource: str) -> list[str]:
    """Reported resource names to try against PRICES, most literal first.

    Production model_name values arrive date-versioned ("claude-sonnet-5-20250929")
    or vendor-qualified by a gateway ("xai/grok-4.5"); PRICES keys are the
    canonical per-provider names. Lowercase, then retry with one trailing
    -YYYYMMDD/-latest suffix stripped, then with the "vendor/" prefix stripped.
    """
    name = resource.lower()
    candidates = [name]
    unversioned = _VERSION_SUFFIX.sub("", name)
    if unversioned != name:
        candidates.append(unversioned)
    for candidate in list(candidates):
        if "/" in candidate:
            candidates.append(candidate.rsplit("/", 1)[-1])
    return candidates


def cost_usd(
    kind: str,
    provider: str,
    resource: str,
    quantity_in: int,
    quantity_out: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> Decimal | None:
    """Cost of the given quantities, or None when the map doesn't price the pair.

    cache_read and cache_write are subsets of quantity_in, billed at their own
    rates when the price entry has them (else at the input rate). The uncached
    remainder is clamped at zero in case a provider ever reports buckets
    exceeding the total.
    """
    price = next(
        (
            hit
            for candidate in _resource_candidates(resource)
            if (hit := PRICES.get((kind, provider.lower(), candidate))) is not None
        ),
        None,
    )
    if price is None:
        return None
    read_rate = price.cache_read_usd_per_mtok
    write_rate = price.cache_write_usd_per_mtok
    uncached_in = max(0, quantity_in - cache_read - cache_write)
    return (
        uncached_in * price.input_usd_per_mtok
        + cache_read * (read_rate if read_rate is not None else price.input_usd_per_mtok)
        + cache_write * (write_rate if write_rate is not None else price.input_usd_per_mtok)
        + quantity_out * price.output_usd_per_mtok
    ) / _MTOK


def usage_rows_from_responses(
    messages: Iterable[ModelMessage], *, key: SessionKey, workflow_id: str
) -> list[dict[str, Any]]:
    """Aggregate a run's ModelResponse messages into usage_events rows.

    One row per (provider, model) seen, summing token counts, with the request
    count in metadata. Returns JSON-ready dicts (sorted for a deterministic
    shape) because they become a recorded step output in the workflow log.
    `provider` and `resource` keep the raw reported provider_name/model_name —
    truthful history; normalization happens only at pricing time in cost_usd.

    metadata.cache_read / metadata.cache_write are SUBSETS of quantity_in
    (pydantic-ai normalizes input_tokens to include both buckets for every
    provider); the keys are omitted when zero.
    """
    totals: dict[tuple[str, str], dict[str, int]] = {}
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        slot = (message.provider_name or "unknown", message.model_name or "unknown")
        entry = totals.setdefault(
            slot, {"in": 0, "out": 0, "requests": 0, "cache_read": 0, "cache_write": 0}
        )
        entry["in"] += message.usage.input_tokens
        entry["out"] += message.usage.output_tokens
        entry["cache_read"] += message.usage.cache_read_tokens
        entry["cache_write"] += message.usage.cache_write_tokens
        entry["requests"] += 1
    return [
        {
            "team": key.team,
            "kind": KIND_MODEL_REQUEST,
            "provider": provider,
            "resource": model,
            "quantity_in": entry["in"],
            "quantity_out": entry["out"],
            "metadata": {
                "requests": entry["requests"],
                **({"cache_read": entry["cache_read"]} if entry["cache_read"] else {}),
                **({"cache_write": entry["cache_write"]} if entry["cache_write"] else {}),
            },
            "channel": key.channel,
            "thread_root": key.thread_root,
            "workflow_id": workflow_id,
        }
        for (provider, model), entry in sorted(totals.items())
    ]


@dataclass(frozen=True)
class RatedLine:
    """One (kind, provider, resource) group of usage, with its money value."""

    kind: str
    provider: str
    resource: str
    quantity_in: int
    quantity_out: int
    cache_read: int
    cache_write: int
    requests: int
    cost_usd: Decimal | None


@dataclass(frozen=True)
class RatedUsage:
    """Rating summary for a batch of usage rows.

    total_cost_usd covers only the priced lines; has_unpriced flags that the
    total understates reality because some line has no price entry.
    """

    lines: list[RatedLine]
    total_cost_usd: Decimal
    has_unpriced: bool


def rate_usage_rows(rows: Iterable[dict[str, Any]]) -> RatedUsage:
    """Turn usage-event rows into money — the only code allowed to do so.

    Takes rows in the shape usage_rows_from_responses produces and the
    usage_events table returns (kind, provider, resource, quantity_in,
    quantity_out, metadata with requests and optional cache_read/cache_write).
    Groups by the RAW (kind, provider, resource) — pricing normalization stays
    inside cost_usd — and sums quantities per group; cost is linear in the
    quantities, so rating the sums equals summing per-row ratings.
    """
    groups: dict[tuple[str, str, str], dict[str, int]] = {}
    for row in rows:
        slot = (row["kind"], row["provider"], row["resource"])
        entry = groups.setdefault(
            slot, {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "requests": 0}
        )
        metadata = row.get("metadata") or {}
        entry["in"] += row["quantity_in"]
        entry["out"] += row["quantity_out"]
        entry["cache_read"] += metadata.get("cache_read", 0)
        entry["cache_write"] += metadata.get("cache_write", 0)
        entry["requests"] += metadata.get("requests", 0)
    lines = [
        RatedLine(
            kind=kind,
            provider=provider,
            resource=resource,
            quantity_in=entry["in"],
            quantity_out=entry["out"],
            cache_read=entry["cache_read"],
            cache_write=entry["cache_write"],
            requests=entry["requests"],
            cost_usd=cost_usd(
                kind,
                provider,
                resource,
                entry["in"],
                entry["out"],
                cache_read=entry["cache_read"],
                cache_write=entry["cache_write"],
            ),
        )
        for (kind, provider, resource), entry in sorted(groups.items())
    ]
    return RatedUsage(
        lines=lines,
        total_cost_usd=sum(
            (line.cost_usd for line in lines if line.cost_usd is not None), Decimal(0)
        ),
        has_unpriced=any(line.cost_usd is None for line in lines),
    )
