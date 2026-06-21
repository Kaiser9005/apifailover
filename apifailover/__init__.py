"""apifailover — a domain-neutral multi-API failover engine.

Try an ordered list of providers; fall back when one raises, times out, or
returns a result your predicate rejects. Supports per-item failover (provider A
fills 8/10 keys, provider B fills the rest) with per-key provenance, plus a
circuit breaker so a chronically-down provider stops getting hammered.

Pure Python, zero dependencies.

Example::

    from apifailover import FailoverClient, Provider

    client = FailoverClient(retries=1)
    result = client.fetch([
        Provider("primary", call_primary),
        Provider("backup", call_backup),
    ])
    print(result.value, "served by", result.provider)
"""

from apifailover.engine import (
    AllProvidersFailed,
    CircuitBreaker,
    FailoverClient,
    ItemProvider,
    ItemResult,
    Provider,
    Result,
)

__version__ = "0.1.0"

__all__ = [
    "FailoverClient",
    "Provider",
    "ItemProvider",
    "Result",
    "ItemResult",
    "CircuitBreaker",
    "AllProvidersFailed",
    "__version__",
]
