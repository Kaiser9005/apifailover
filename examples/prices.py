#!/usr/bin/env python3
"""Runnable, network-free example of per-item failover.

Two fake "price providers" stand in for real APIs. The primary is flaky and
only knows some symbols; the secondary fills the gaps. Every value is tagged
with the provider that served it.

Run it::

    python3 examples/prices.py

No API keys, no network, no secrets — this is a deterministic demo of the
shape you'd use against any pair of real endpoints (a primary feed plus a
broader backup, two weather APIs, two geocoders, two LLM gateways, …).
"""

import os
import sys

# Make the example runnable straight from the repo (``python3 examples/prices.py``)
# without an editable install — put the repo root on the path if needed.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apifailover import CircuitBreaker, FailoverClient, ItemProvider


# --- Fake provider #1: knows majors, but "rate-limits" on the long tail ----- #
def primary_prices(symbols):
    catalogue = {"BTC": 64000.0, "ETH": 3100.0, "SOL": 145.0}
    served = {s: catalogue[s] for s in symbols if s in catalogue}
    if not served:
        # Pretend the upstream returned a 429 for unknown symbols.
        raise RuntimeError("primary: rate-limited / symbol not listed")
    return served


# --- Fake provider #2: broader catalogue, used only for the gaps ------------ #
def backup_prices(symbols):
    catalogue = {
        "BTC": 63950.0,
        "ETH": 3098.0,
        "SOL": 144.5,
        "AVAX": 38.2,
        "MATIC": 0.72,
    }
    return {s: catalogue[s] for s in symbols if s in catalogue}


def main() -> None:
    wanted = ["BTC", "ETH", "SOL", "AVAX", "MATIC", "DOGE"]

    client = FailoverClient(
        retries=1,
        is_valid=lambda price: price is not None and price > 0,
        circuit_breaker=CircuitBreaker(failure_threshold=3, reset_after=30.0),
    )

    result = client.fetch_items(
        wanted,
        [
            ItemProvider("primary", primary_prices),
            ItemProvider("backup", backup_prices),
        ],
    )

    print("Per-item failover result")
    print("=" * 48)
    for symbol in wanted:
        if symbol in result.values:
            src = result.provenance[symbol]
            print(f"  {symbol:<6} {result.values[symbol]:>12,.2f}   (via {src})")
        else:
            print(f"  {symbol:<6} {'—':>12}   (no provider had it)")

    print("-" * 48)
    print(f"  providers queried : {', '.join(result.attempts)}")
    print(f"  still missing     : {result.missing or 'none'}")

    served_by_backup = [s for s, src in result.provenance.items() if src == "backup"]
    print(f"  filled by backup  : {served_by_backup}")


if __name__ == "__main__":
    main()
