"""Usage aggregation and pricing unit tests."""

from decimal import Decimal

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from hondo.models import SessionKey
from hondo.usage import (
    KIND_MODEL_REQUEST,
    ModelPrice,
    cost_usd,
    rate_usage_rows,
    usage_rows_from_responses,
)

KEY = SessionKey(team="T01", channel="C01", thread_root="1700000000.000100")

# Pricing-LOGIC tests run against this made-up map so they don't depend on
# which real models the shipping map happens to price. Tests of the real map
# (the grok entry we actually ship) deliberately do not use the fixture.
SYNTHETIC_PRICES = {
    (KIND_MODEL_REQUEST, "acme", "example-model"): ModelPrice(
        Decimal("4"), Decimal("20"), Decimal("0.40"), Decimal("5")
    ),
    # Vendor-qualified id, the shape gateways report: matched literally.
    (KIND_MODEL_REQUEST, "acme", "vendor/gated-model"): ModelPrice(Decimal("2"), Decimal("8")),
}


@pytest.fixture
def synthetic_prices(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hondo.usage.PRICES", SYNTHETIC_PRICES)


def response(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    provider: str | None = None,
    cache_read: int = 0,
    cache_write: int = 0,
) -> ModelResponse:
    return ModelResponse(
        parts=[TextPart("ok")],
        usage=RequestUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
        ),
        model_name=model,
        provider_name=provider,
    )


def test_rows_group_by_provider_and_model_and_sum_tokens() -> None:
    messages = [
        ModelRequest(parts=[UserPromptPart("hi")]),
        response("claude-sonnet-5", 100, 10, provider="anthropic"),
        response("claude-sonnet-5", 200, 20, provider="anthropic"),
        response("claude-haiku-4-5", 50, 5, provider="anthropic"),
    ]
    rows = usage_rows_from_responses(messages, key=KEY, workflow_id="WF1")
    assert rows == [
        {
            "team": "T01",
            "kind": KIND_MODEL_REQUEST,
            "provider": "anthropic",
            "resource": "claude-haiku-4-5",
            "quantity_in": 50,
            "quantity_out": 5,
            "metadata": {"requests": 1},
            "channel": "C01",
            "thread_root": "1700000000.000100",
            "workflow_id": "WF1",
        },
        {
            "team": "T01",
            "kind": KIND_MODEL_REQUEST,
            "provider": "anthropic",
            "resource": "claude-sonnet-5",
            "quantity_in": 300,
            "quantity_out": 30,
            "metadata": {"requests": 2},
            "channel": "C01",
            "thread_root": "1700000000.000100",
            "workflow_id": "WF1",
        },
    ]


def test_same_model_under_two_providers_stays_apart() -> None:
    rows = usage_rows_from_responses(
        [
            response("grok-4.5", 10, 1, provider="xai"),
            response("grok-4.5", 20, 2, provider="vercel"),
        ],
        key=KEY,
        workflow_id="WF1",
    )
    assert [(row["provider"], row["resource"]) for row in rows] == [
        ("vercel", "grok-4.5"),
        ("xai", "grok-4.5"),
    ]


def test_rows_sum_cache_buckets_into_metadata() -> None:
    rows = usage_rows_from_responses(
        [
            response(
                "claude-sonnet-5", 1000, 10, provider="anthropic", cache_read=200, cache_write=300
            ),
            response("claude-sonnet-5", 500, 5, provider="anthropic", cache_read=100),
        ],
        key=KEY,
        workflow_id="WF1",
    )
    # Cache buckets are subsets of quantity_in, never additions to it.
    assert rows[0]["quantity_in"] == 1500
    assert rows[0]["metadata"] == {"requests": 2, "cache_read": 300, "cache_write": 300}


def test_cache_metadata_keys_are_omitted_when_zero() -> None:
    rows = usage_rows_from_responses(
        [response("claude-sonnet-5", 100, 10, provider="anthropic")], key=KEY, workflow_id="WF1"
    )
    assert rows[0]["metadata"] == {"requests": 1}


def test_missing_names_fall_back_to_unknown() -> None:
    rows = usage_rows_from_responses([response(None, 10, 1)], key=KEY, workflow_id="WF1")
    assert [(row["provider"], row["resource"]) for row in rows] == [("unknown", "unknown")]


def test_no_responses_yield_no_rows() -> None:
    messages = [ModelRequest(parts=[UserPromptPart("hi")])]
    assert usage_rows_from_responses(messages, key=KEY, workflow_id="WF1") == []


def test_cost_for_a_priced_model(synthetic_prices: None) -> None:
    cost = cost_usd(KIND_MODEL_REQUEST, "acme", "example-model", 1_000_000, 2_000_000)
    assert cost == Decimal("4") + 2 * Decimal("20")
    assert isinstance(cost, Decimal)


def test_unknown_model_reports_tokens_but_no_cost() -> None:
    # The row still exists (tokens are reported); only the price is unknown.
    rows = usage_rows_from_responses(
        [response("some-new-model", 42, 7, provider="vercel")], key=KEY, workflow_id="WF1"
    )
    assert rows[0]["quantity_in"] == 42
    assert cost_usd(KIND_MODEL_REQUEST, "vercel", "some-new-model", 42, 7) is None


def test_cost_normalizes_real_world_model_names(synthetic_prices: None) -> None:
    """Date-versioned, -latest, odd-cased, and vendor-prefixed names all price."""
    expected = cost_usd(KIND_MODEL_REQUEST, "acme", "example-model", 100, 10)
    assert expected is not None
    for provider, reported in (
        ("acme", "example-model-20250929"),
        ("acme", "example-model-latest"),
        ("ACME", "EXAMPLE-MODEL"),
        ("acme", "somevendor/example-model"),
        ("acme", "somevendor/example-model-20250929"),
    ):
        assert cost_usd(KIND_MODEL_REQUEST, provider, reported, 100, 10) == expected, (
            provider,
            reported,
        )


def test_vendor_qualified_price_keys_match_literally(synthetic_prices: None) -> None:
    # The key keeps the vendor prefix; the literal report hits before any stripping.
    assert cost_usd(KIND_MODEL_REQUEST, "acme", "vendor/gated-model", 1_000_000, 0) == Decimal("2")


def test_cost_prices_the_vercel_gateway_grok_deployment() -> None:
    """The production shape: provider "vercel", resource = the gateway's own
    vendor-qualified model id — priced exactly, plus case/date variants."""
    expected = cost_usd(KIND_MODEL_REQUEST, "vercel", "xai/grok-4.5", 1_000_000, 1_000_000)
    assert expected == Decimal("2") + Decimal("6")
    for provider, reported in (
        ("Vercel", "XAI/GROK-4.5"),
        ("vercel", "xai/grok-4.5-20260701"),
        ("vercel", "xai/grok-4.5-latest"),
    ):
        assert cost_usd(KIND_MODEL_REQUEST, provider, reported, 1_000_000, 1_000_000) == expected, (
            provider,
            reported,
        )


def test_cached_reads_bill_at_the_cache_rate() -> None:
    full = cost_usd(KIND_MODEL_REQUEST, "vercel", "xai/grok-4.5", 1_000_000, 0)
    cached = cost_usd(
        KIND_MODEL_REQUEST, "vercel", "xai/grok-4.5", 1_000_000, 0, cache_read=1_000_000
    )
    assert full == Decimal("2")
    assert cached == Decimal("0.50")


def test_missing_cache_rate_bills_at_the_input_rate() -> None:
    # xAI has no cache-write premium (write rate is None): a write-heavy call
    # costs exactly what the old cache-blind formula said.
    plain = cost_usd(KIND_MODEL_REQUEST, "vercel", "xai/grok-4.5", 1_000_000, 500)
    written = cost_usd(
        KIND_MODEL_REQUEST, "vercel", "xai/grok-4.5", 1_000_000, 500, cache_write=400_000
    )
    assert written == plain


def test_cache_rates_split_the_input_bucket(synthetic_prices: None) -> None:
    cost = cost_usd(
        KIND_MODEL_REQUEST,
        "acme",
        "example-model",
        1_000_000,
        0,
        cache_read=500_000,
        cache_write=200_000,
    )
    # 300K uncached at 4 + 500K read at 0.40 + 200K write at 5, per Mtok.
    assert cost == Decimal("2.4")


def test_inconsistent_cache_buckets_clamp_the_uncached_remainder() -> None:
    # Buckets exceeding the reported total must not bill a negative remainder.
    cost = cost_usd(KIND_MODEL_REQUEST, "vercel", "xai/grok-4.5", 100, 0, cache_read=150)
    assert cost == Decimal("150") * Decimal("0.50") / Decimal(1_000_000)


def test_unknown_provider_costs_none_even_for_a_known_model(synthetic_prices: None) -> None:
    # Prices are per provider; a model priced under one provider says nothing
    # about another (or an unreported) one.
    assert cost_usd(KIND_MODEL_REQUEST, "unknown", "example-model", 100, 10) is None
    assert cost_usd(KIND_MODEL_REQUEST, "other", "vendor/gated-model", 100, 10) is None


def test_unrouted_provider_costs_none_on_the_real_map() -> None:
    # The shipping map prices only the gateway; the same model id through
    # another route stays unpriced.
    assert cost_usd(KIND_MODEL_REQUEST, "openrouter", "xai/grok-4.5", 100, 10) is None


def test_cost_normalization_never_invents_a_price() -> None:
    assert cost_usd(KIND_MODEL_REQUEST, "vercel", "xai/grok-9-20270101", 100, 10) is None
    # Observed in production through the Vercel gateway; deliberately unpriced
    # until a trustworthy rate lands — tokens recorded, cost None.
    assert cost_usd(KIND_MODEL_REQUEST, "vercel", "google/gemini-3.6-flash", 100, 10) is None


def test_rows_keep_the_raw_reported_names() -> None:
    # Truthful history: normalization is a pricing-time concern only.
    rows = usage_rows_from_responses(
        [response("XAI/GROK-4.5-20260701", 10, 1, provider="Vercel")],
        key=KEY,
        workflow_id="WF1",
    )
    assert [(row["provider"], row["resource"]) for row in rows] == [
        ("Vercel", "XAI/GROK-4.5-20260701")
    ]


def test_rate_usage_rows_mixed_priced_and_unpriced() -> None:
    rows = usage_rows_from_responses(
        [
            response("xai/grok-4.5", 1_000_000, 1_000_000, provider="vercel"),
            response("google/gemini-3.6-flash", 500, 50, provider="vercel"),
        ],
        key=KEY,
        workflow_id="WF1",
    )
    rated = rate_usage_rows(rows)
    gemini, grok = rated.lines
    assert (gemini.provider, gemini.resource, gemini.cost_usd) == (
        "vercel",
        "google/gemini-3.6-flash",
        None,
    )
    assert grok.cost_usd == Decimal("8")  # 1M in at 2 + 1M out at 6
    # The total covers only the priced lines; the flag says it understates.
    assert rated.total_cost_usd == Decimal("8")
    assert rated.has_unpriced is True


def test_rate_usage_rows_prices_cache_buckets_from_metadata() -> None:
    rows = usage_rows_from_responses(
        [response("xai/grok-4.5", 1_000_000, 0, provider="vercel", cache_read=1_000_000)],
        key=KEY,
        workflow_id="WF1",
    )
    rated = rate_usage_rows(rows)
    (line,) = rated.lines
    assert line.cache_read == 1_000_000
    assert line.cost_usd == Decimal("0.50")  # fully cached read, no full-rate input
    assert rated.total_cost_usd == Decimal("0.50")
    assert rated.has_unpriced is False


def test_rate_usage_rows_groups_rows_across_workflows() -> None:
    # DB-shaped input: several workflows' rows for the same (provider, model)
    # merge into one line with summed quantities and requests.
    rows = [
        *usage_rows_from_responses(
            [response("claude-sonnet-5", 100, 10, provider="anthropic")],
            key=KEY,
            workflow_id="WF1",
        ),
        *usage_rows_from_responses(
            [response("claude-sonnet-5", 200, 20, provider="anthropic")],
            key=KEY,
            workflow_id="WF2",
        ),
    ]
    rated = rate_usage_rows(rows)
    (line,) = rated.lines
    assert (line.quantity_in, line.quantity_out, line.requests) == (300, 30, 2)


def test_rate_usage_rows_empty_input() -> None:
    rated = rate_usage_rows([])
    assert rated.lines == []
    assert rated.total_cost_usd == Decimal(0)
    assert rated.has_unpriced is False
