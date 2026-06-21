"""Offline tests for the apifailover engine.

No network. All "providers" are plain Python callables that succeed, fail, or
return partial mappings. Time and sleep are injected so circuit-breaker and
timeout behaviour is deterministic.
"""

import pytest

from apifailover import (
    AllProvidersFailed,
    CircuitBreaker,
    FailoverClient,
    ItemProvider,
    Provider,
)


# --------------------------------------------------------------------------- #
# Fake provider helpers
# --------------------------------------------------------------------------- #
def boom():
    raise ConnectionError("provider down")


class FakeClock:
    """Manually-advanced monotonic clock for deterministic time tests."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# --------------------------------------------------------------------------- #
# Whole-call failover
# --------------------------------------------------------------------------- #
def test_first_provider_succeeds():
    client = FailoverClient()
    result = client.fetch(
        [Provider("primary", lambda: "ok"), Provider("backup", boom)]
    )
    assert result.value == "ok"
    assert result.provider == "primary"
    assert result.attempts == ["primary"]  # backup never tried


def test_falls_back_to_second_provider():
    client = FailoverClient()
    result = client.fetch(
        [Provider("primary", boom), Provider("backup", lambda: 42)]
    )
    assert result.value == 42
    assert result.provider == "backup"
    assert result.attempts == ["primary", "backup"]


def test_all_providers_fail_raises_with_causes():
    client = FailoverClient()
    with pytest.raises(AllProvidersFailed) as excinfo:
        client.fetch([Provider("a", boom), Provider("b", boom)])
    # Each provider's root cause is preserved for inspection.
    assert set(excinfo.value.errors) == {"a", "b"}
    assert isinstance(excinfo.value.errors["a"], ConnectionError)


def test_empty_providers_raises_value_error():
    client = FailoverClient()
    with pytest.raises(ValueError):
        client.fetch([])


# --------------------------------------------------------------------------- #
# is_valid predicate
# --------------------------------------------------------------------------- #
def test_is_valid_rejects_bad_result_and_falls_back():
    # First provider "succeeds" but returns an empty payload we consider bad.
    client = FailoverClient(is_valid=lambda r: bool(r))
    result = client.fetch(
        [Provider("primary", lambda: ""), Provider("backup", lambda: "real")]
    )
    assert result.value == "real"
    assert result.provider == "backup"


def test_default_is_valid_rejects_none():
    client = FailoverClient()
    result = client.fetch(
        [Provider("primary", lambda: None), Provider("backup", lambda: "x")]
    )
    assert result.provider == "backup"


# --------------------------------------------------------------------------- #
# Retries
# --------------------------------------------------------------------------- #
def test_retries_same_provider_before_failover():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("transient")
        return "recovered"

    # retries=2 → up to 3 attempts on the same provider.
    client = FailoverClient(retries=2)
    result = client.fetch([Provider("flaky", flaky)])
    assert result.value == "recovered"
    assert calls["n"] == 3


def test_retry_backoff_sleeps_between_attempts():
    slept = []
    client = FailoverClient(
        retries=2, retry_backoff=0.5, sleep=lambda s: slept.append(s)
    )
    with pytest.raises(AllProvidersFailed):
        client.fetch([Provider("a", boom)])
    # Linear backoff: 0.5 * 1, then 0.5 * 2 (no sleep after the final attempt).
    assert slept == [0.5, 1.0]


# --------------------------------------------------------------------------- #
# Timeout (wall-clock, via injected clock)
# --------------------------------------------------------------------------- #
def test_slow_provider_treated_as_failure():
    clock = FakeClock()

    def slow():
        clock.advance(5.0)  # pretend the call took 5s
        return "too-late"

    client = FailoverClient(timeout=1.0, clock=clock)
    result = client.fetch(
        [Provider("slow", slow), Provider("fast", lambda: "on-time")]
    )
    assert result.value == "on-time"
    assert result.provider == "fast"


# --------------------------------------------------------------------------- #
# Per-item failover (the differentiator)
# --------------------------------------------------------------------------- #
def test_per_item_fill_from_second_provider():
    # Provider A serves 2 of 3 keys; provider B fills the missing one.
    provider_a = ItemProvider("a", lambda keys: {"BTC": 1.0, "ETH": 2.0})
    provider_b = ItemProvider("b", lambda keys: {k: 9.9 for k in keys})

    client = FailoverClient()
    res = client.fetch_items(["BTC", "ETH", "SOL"], [provider_a, provider_b])

    assert res.values == {"BTC": 1.0, "ETH": 2.0, "SOL": 9.9}
    assert res.provenance["BTC"] == "a"
    assert res.provenance["ETH"] == "a"
    assert res.provenance["SOL"] == "b"  # filled by fallback
    assert res.missing == []
    assert res.attempts == ["a", "b"]


def test_per_item_second_provider_only_asked_for_missing_keys():
    seen = {}

    def b_call(keys):
        seen["keys"] = list(keys)
        return {k: 0.0 for k in keys}

    provider_a = ItemProvider("a", lambda keys: {"X": 1.0})
    provider_b = ItemProvider("b", b_call)

    client = FailoverClient()
    client.fetch_items(["X", "Y", "Z"], [provider_a, provider_b])
    # B must only be asked for what A failed to serve.
    assert seen["keys"] == ["Y", "Z"]


def test_per_item_keys_still_missing_are_reported():
    provider_a = ItemProvider("a", lambda keys: {"X": 1.0})
    client = FailoverClient()
    res = client.fetch_items(["X", "Y"], [provider_a])
    assert res.values == {"X": 1.0}
    assert res.missing == ["Y"]


def test_per_item_require_all_raises_when_incomplete():
    provider_a = ItemProvider("a", lambda keys: {"X": 1.0})
    client = FailoverClient()
    with pytest.raises(AllProvidersFailed):
        client.fetch_items(["X", "Y"], [provider_a], require_all=True)


def test_per_item_is_valid_rejects_value_and_fills_from_next():
    # Provider A returns a value for SOL that the predicate rejects (<= 0);
    # the key must stay open and get filled by B. The mapping itself is never
    # subjected to the per-value predicate (regression guard: a value-level
    # is_valid must not be called on the whole dict).
    provider_a = ItemProvider("a", lambda keys: {"BTC": 100.0, "SOL": 0.0})
    provider_b = ItemProvider("b", lambda keys: {k: 5.0 for k in keys})

    client = FailoverClient(is_valid=lambda price: price > 0)
    res = client.fetch_items(["BTC", "SOL"], [provider_a, provider_b])

    assert res.values == {"BTC": 100.0, "SOL": 5.0}
    assert res.provenance["BTC"] == "a"
    assert res.provenance["SOL"] == "b"  # A's 0.0 was rejected, filled by B


def test_per_item_failing_provider_is_skipped():
    def a_boom(keys):
        raise ConnectionError("A is down")

    provider_a = ItemProvider("a", a_boom)
    provider_b = ItemProvider("b", lambda keys: {k: 7 for k in keys})

    client = FailoverClient()
    res = client.fetch_items(["P", "Q"], [provider_a, provider_b])
    assert res.values == {"P": 7, "Q": 7}
    assert all(src == "b" for src in res.provenance.values())


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
def test_circuit_breaker_trips_and_skips_provider():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, reset_after=30.0, clock=clock)
    client = FailoverClient(circuit_breaker=breaker, clock=clock)

    good = {"calls": 0}

    def primary():
        raise ConnectionError("down")

    def backup():
        good["calls"] += 1
        return "ok"

    providers = [Provider("primary", primary), Provider("backup", backup)]

    # Two failures of "primary" trip its breaker (threshold=2).
    client.fetch(providers)
    client.fetch(providers)
    assert breaker.state("primary") == "open"

    # Next call: primary is skipped entirely — its body must not run again.
    result = client.fetch(providers)
    assert result.provider == "backup"
    assert breaker.is_open("primary") is True


def test_circuit_breaker_half_open_then_recovers():
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, reset_after=10.0, clock=clock)
    client = FailoverClient(circuit_breaker=breaker, clock=clock)

    state = {"up": False}

    def primary():
        if not state["up"]:
            raise ConnectionError("down")
        return "primary-back"

    providers = [Provider("primary", primary), Provider("backup", lambda: "backup")]

    client.fetch(providers)  # 1 failure → opens (threshold=1)
    assert breaker.is_open("primary") is True

    clock.advance(11.0)  # cooldown elapsed → half-open probe allowed
    assert breaker.state("primary") == "half-open"

    state["up"] = True  # primary recovers
    result = client.fetch(providers)
    assert result.provider == "primary"
    assert breaker.state("primary") == "closed"


def test_circuit_breaker_success_resets_failure_count():
    breaker = CircuitBreaker(failure_threshold=3)
    breaker.record_failure("x")
    breaker.record_failure("x")
    breaker.record_success("x")  # streak broken
    breaker.record_failure("x")
    breaker.record_failure("x")
    # Only 2 consecutive failures since the success → still closed.
    assert breaker.is_open("x") is False


# --------------------------------------------------------------------------- #
# Validation guards
# --------------------------------------------------------------------------- #
def test_invalid_construction_args_rejected():
    with pytest.raises(ValueError):
        FailoverClient(retries=-1)
    with pytest.raises(ValueError):
        FailoverClient(timeout=0)
    with pytest.raises(ValueError):
        CircuitBreaker(failure_threshold=0)
