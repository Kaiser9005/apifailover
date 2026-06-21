# apifailover

**Your integration stops dying when one API has a bad day.**

When the API you depend on rate-limits, times out, or returns garbage, your code
shouldn't go down with it. `apifailover` takes an ordered list of providers and
tries them in order — and when a provider answers only *some* of what you asked
for, it fills the rest from the next one. Every value comes back tagged with
which provider served it.

Pure Python. **Zero dependencies.** Fully type-hinted. Works with *any* callable
— prices, weather, geocoding, sports data, LLM endpoints — it doesn't know or
care about your domain.

> **Package name:** published as `apifailover` (verified available on PyPI at
> time of writing). If it's been claimed by the time you publish, good
> alternatives — also available — are **`failover-engine`** and
> **`multifailover`**. (`apiguard` is taken.) Pick one and update `name` in
> `pyproject.toml` + the install line below.

## Why this exists

Everyone reinvents this badly: a bare `try/except` that swaps to a backup, no
provenance, naive infinite retry that hammers a dead endpoint. This is the
reusable version, with the two things people skip:

- **Per-item failover** — provider A returns 8 of 10 items, provider B fills the
  2 missing ones. You get all 10, each tagged with its source.
- **A circuit breaker** — a provider that keeps failing is *skipped* for a cooldown
  instead of being retried forever, then probed once to see if it's back.

## Install

```bash
pip install apifailover
```

## Quick start

```python
from apifailover import FailoverClient, Provider

client = FailoverClient(retries=1)  # one retry per provider before failover

result = client.fetch([
    Provider("primary", lambda: call_primary_api()),
    Provider("backup",  lambda: call_backup_api()),
])

print(result.value)      # whatever the winning provider returned
print(result.provider)   # "primary" or "backup" — provenance, for free
print(result.attempts)   # ["primary"] or ["primary", "backup"]
```

If every provider fails you get one `AllProvidersFailed` that preserves each
provider's root cause (`.errors` is a `{name: exception}` mapping) — never a
single opaque error.

## The differentiator: per-item failover

This is the feature most failover snippets don't have. Ask for a set of keys;
each provider serves what it can; the next provider is asked **only for what's
still missing**.

```python
from apifailover import FailoverClient, ItemProvider

# Two fake price feeds. Primary knows the majors; backup has the long tail.
def primary(symbols):
    book = {"BTC": 64000.0, "ETH": 3100.0}
    return {s: book[s] for s in symbols if s in book}

def backup(symbols):
    book = {"BTC": 63950.0, "AVAX": 38.2, "MATIC": 0.72}
    return {s: book[s] for s in symbols if s in book}

client = FailoverClient()
res = client.fetch_items(
    ["BTC", "ETH", "AVAX"],
    [ItemProvider("primary", primary), ItemProvider("backup", backup)],
)

res.values       # {"BTC": 64000.0, "ETH": 3100.0, "AVAX": 38.2}
res.provenance   # {"BTC": "primary", "ETH": "primary", "AVAX": "backup"}
res.missing      # []   (use require_all=True to raise instead)
```

`backup` was only ever asked for `AVAX` — the key `primary` couldn't serve.

## The circuit breaker

No naive infinite retry. After N consecutive failures a provider's circuit
**opens** and it's skipped for a cooldown window; the first call after the window
is a half-open probe — success closes it, failure re-opens it.

```python
from apifailover import FailoverClient, Provider, CircuitBreaker

breaker = CircuitBreaker(failure_threshold=3, reset_after=60.0)
client = FailoverClient(circuit_breaker=breaker)

providers = [Provider("flaky", call_flaky), Provider("stable", call_stable)]

for _ in range(100):
    result = client.fetch(providers)
    # Once "flaky" has failed 3× in a row it's skipped for 60s — the request
    # goes straight to "stable" instead of paying the timeout every time.
    breaker.state("flaky")  # "closed" | "open" | "half-open"
```

## Reject bad-but-non-throwing results

Some APIs return `200 OK` with junk (empty list, `null`, a stale sentinel).
Give `is_valid` a predicate; a result that fails it is treated as a failure and
triggers failover:

```python
client = FailoverClient(is_valid=lambda r: bool(r))      # reject empty/None
client = FailoverClient(is_valid=lambda price: price > 0) # reject non-positive
```

For per-item calls, `is_valid` is applied per key — a provider's bad value for
one key just leaves that key open for the next provider.

## Knobs

| `FailoverClient(...)`  | Default | What it does |
|------------------------|---------|--------------|
| `retries`              | `0`     | Extra attempts on the **same** provider before failing over. |
| `retry_backoff`        | `0.0`   | Seconds slept between same-provider retries (scales linearly). |
| `timeout`              | `None`  | Wall-clock soft deadline per provider; a slower return counts as a failure. |
| `is_valid`             | not-`None` | Predicate to reject bad-but-non-throwing results. |
| `circuit_breaker`      | `None`  | A `CircuitBreaker` to skip chronically-failing providers. |

## Scope & honesty

What this **is**: a small, dependency-free, synchronous failover/fallback layer
you wrap around your own HTTP calls (or any callable).

What it is **not** (by design, to stay small and honest):

- **Not async.** Providers are called synchronously. Wrap blocking calls in your
  own executor if you need concurrency.
- **Not a hard timeout.** `timeout` is a wall-clock check *after* the call
  returns — it can't interrupt a call mid-flight (that needs threads/signals and
  is intentionally out of scope). It still protects you from a slow provider
  winning when a faster fallback exists.
- **Not an HTTP client.** Bring your own `requests`/`httpx`; this just decides
  *which* provider's result to use and falls back when one misbehaves.

## Run the tests

```bash
pip install pytest
python3 -m pytest tests/ -q     # 18 tests, fully offline
```

## License

MIT © 2026 Ivan Fodjo
