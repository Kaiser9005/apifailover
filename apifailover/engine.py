"""Core failover engine.

A domain-neutral multi-provider failover client. Give it an ordered list of
providers (any callable + a name); it tries them in order and falls back to the
next when one raises, times out, or returns a result your predicate rejects.

The differentiated feature is *per-item* failover: when a provider answers only
some of the requested keys, the remaining keys are filled from the next
provider — and every served value is tagged with which provider produced it
(provenance).

Zero third-party dependencies. Works with any callable, sync only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import (
    Callable,
    Dict,
    Generic,
    Hashable,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    TypeVar,
)

__all__ = [
    "Provider",
    "ItemProvider",
    "Result",
    "ItemResult",
    "AllProvidersFailed",
    "CircuitBreaker",
    "FailoverClient",
]

T = TypeVar("T")
K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #
class AllProvidersFailed(Exception):
    """Raised when every provider failed (or was skipped) for a call.

    The per-provider errors are preserved in ``errors`` so callers can inspect
    exactly why each one failed instead of getting a single opaque exception.
    """

    def __init__(self, errors: Mapping[str, BaseException]):
        self.errors: Dict[str, BaseException] = dict(errors)
        detail = ", ".join(f"{name}: {err!r}" for name, err in self.errors.items())
        super().__init__(f"all providers failed ({detail or 'no providers tried'})")


# --------------------------------------------------------------------------- #
# Provider descriptors
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Provider(Generic[T]):
    """A whole-call provider: ``call()`` returns the full result or raises.

    Args:
        name: Stable identifier used in provenance and the circuit breaker.
        call: Zero-argument callable returning the result.
    """

    name: str
    call: Callable[[], T]


@dataclass(frozen=True)
class ItemProvider(Generic[K, V]):
    """A per-item provider for :meth:`FailoverClient.fetch_items`.

    ``call(keys)`` receives the keys still missing and returns a mapping for
    whichever subset it can serve (it need not return every key).

    Args:
        name: Stable identifier used in provenance and the circuit breaker.
        call: Callable taking the still-missing keys, returning a partial mapping.
    """

    name: str
    call: Callable[[Sequence[K]], Mapping[K, V]]


# --------------------------------------------------------------------------- #
# Results (tagged with provenance)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Result(Generic[T]):
    """Outcome of a whole-call failover, tagged with which provider served it.

    Args:
        value: The value returned by the winning provider.
        provider: Name of the provider that produced ``value``.
        attempts: Ordered names of every provider tried, including the winner.
    """

    value: T
    provider: str
    attempts: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class ItemResult(Generic[K, V]):
    """Outcome of a per-item failover.

    Args:
        values: Merged mapping of every key that any provider served.
        provenance: For each served key, the name of the provider that filled it.
        missing: Keys no provider was able to serve.
        attempts: Ordered names of every provider that was queried.
    """

    values: Dict[K, V]
    provenance: Dict[K, str]
    missing: List[K] = field(default_factory=list)
    attempts: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #
class CircuitBreaker:
    """Skip a provider that has failed too often, recently.

    After ``failure_threshold`` consecutive failures a provider is "open"
    (skipped) for ``reset_after`` seconds. The first call after that window is a
    half-open probe: success closes the circuit, failure re-opens it. This
    replaces naive infinite retry — a chronically-down provider stops being
    hammered while still getting periodic probes.

    Args:
        failure_threshold: Consecutive failures before the circuit opens.
        reset_after: Seconds the circuit stays open before a half-open probe.
        clock: Time source (seconds). Injectable for deterministic tests.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        reset_after: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if reset_after < 0:
            raise ValueError("reset_after must be >= 0")
        self.failure_threshold = failure_threshold
        self.reset_after = reset_after
        self._clock = clock
        self._failures: Dict[str, int] = {}
        self._opened_at: Dict[str, float] = {}

    def is_open(self, name: str) -> bool:
        """Return True if ``name`` should currently be skipped."""
        opened = self._opened_at.get(name)
        if opened is None:
            return False
        if self._clock() - opened >= self.reset_after:
            # Cooldown elapsed: allow a half-open probe on the next attempt.
            return False
        return True

    def record_success(self, name: str) -> None:
        """Reset the breaker for ``name`` after a successful call."""
        self._failures.pop(name, None)
        self._opened_at.pop(name, None)

    def record_failure(self, name: str) -> None:
        """Count a failure for ``name``; open the circuit at the threshold."""
        count = self._failures.get(name, 0) + 1
        self._failures[name] = count
        if count >= self.failure_threshold:
            self._opened_at[name] = self._clock()

    def state(self, name: str) -> str:
        """Return ``'closed'``, ``'open'``, or ``'half-open'`` for inspection."""
        if name not in self._opened_at:
            return "closed"
        return "open" if self.is_open(name) else "half-open"


# --------------------------------------------------------------------------- #
# Failover client
# --------------------------------------------------------------------------- #
class FailoverClient:
    """Try providers in order, falling back on failure.

    Args:
        retries: Extra attempts per provider before failing over (0 = try once).
        retry_backoff: Seconds to sleep between retries of the same provider.
            The delay scales linearly with the attempt number.
        timeout: Per-provider soft deadline in seconds. A provider that returns
            *after* this many seconds is treated as a failure (it cannot be
            interrupted mid-call without threads; this is a wall-clock check).
        is_valid: Predicate on a successful result; returning ``False`` treats
            the result as a failure and triggers failover. Defaults to
            "any non-``None`` result is valid".
        circuit_breaker: Optional :class:`CircuitBreaker`. When set, open
            providers are skipped.
        sleep: Sleep function (seconds). Injectable for deterministic tests.
        clock: Time source (seconds). Injectable for deterministic tests.
    """

    def __init__(
        self,
        retries: int = 0,
        retry_backoff: float = 0.0,
        timeout: Optional[float] = None,
        is_valid: Optional[Callable[[object], bool]] = None,
        circuit_breaker: Optional[CircuitBreaker] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if retries < 0:
            raise ValueError("retries must be >= 0")
        if retry_backoff < 0:
            raise ValueError("retry_backoff must be >= 0")
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be > 0 when set")
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.timeout = timeout
        self.is_valid: Callable[[object], bool] = (
            is_valid if is_valid is not None else (lambda r: r is not None)
        )
        self.circuit_breaker = circuit_breaker
        self._sleep = sleep
        self._clock = clock

    # -- whole-call failover ------------------------------------------------ #
    def fetch(self, providers: Sequence[Provider[T]]) -> Result[T]:
        """Return the first valid result across ``providers``, in order.

        Args:
            providers: Ordered providers; earlier ones are preferred.

        Returns:
            A :class:`Result` tagged with the winning provider's name.

        Raises:
            AllProvidersFailed: If no provider yields a valid result.
            ValueError: If ``providers`` is empty.
        """
        if not providers:
            raise ValueError("providers must be a non-empty sequence")

        attempts: List[str] = []
        errors: Dict[str, BaseException] = {}

        for provider in providers:
            if self.circuit_breaker is not None and self.circuit_breaker.is_open(
                provider.name
            ):
                errors[provider.name] = RuntimeError("circuit open; provider skipped")
                continue

            attempts.append(provider.name)
            try:
                value = self._call_with_retries(provider.name, provider.call)
            except _ProviderError as exc:
                errors[provider.name] = exc.cause
                continue
            return Result(value=value, provider=provider.name, attempts=attempts)

        raise AllProvidersFailed(errors)

    # -- per-item failover -------------------------------------------------- #
    def fetch_items(
        self,
        keys: Iterable[K],
        providers: Sequence[ItemProvider[K, V]],
        *,
        require_all: bool = False,
    ) -> ItemResult[K, V]:
        """Fill ``keys`` across providers, item by item.

        Each provider is asked only for the keys still missing. Values are
        merged and tagged with the provider that produced them. A provider that
        raises (or is circuit-open) is skipped and the next is tried for the
        same outstanding keys.

        Args:
            keys: The keys to fetch (order preserved, duplicates removed).
            providers: Ordered per-item providers; earlier ones are preferred.
            require_all: If True, raise :class:`AllProvidersFailed` when any key
                is still missing after every provider has been tried.

        Returns:
            An :class:`ItemResult` with merged values, per-key provenance, and
            any keys still missing.

        Raises:
            AllProvidersFailed: If ``require_all`` is set and keys remain
                missing, or if no provider could serve anything.
            ValueError: If ``providers`` is empty.
        """
        if not providers:
            raise ValueError("providers must be a non-empty sequence")

        # Preserve order, drop duplicates.
        outstanding: List[K] = list(dict.fromkeys(keys))
        values: Dict[K, V] = {}
        provenance: Dict[K, str] = {}
        attempts: List[str] = []
        errors: Dict[str, BaseException] = {}

        for provider in providers:
            if not outstanding:
                break
            if self.circuit_breaker is not None and self.circuit_breaker.is_open(
                provider.name
            ):
                errors[provider.name] = RuntimeError("circuit open; provider skipped")
                continue

            attempts.append(provider.name)
            requested = tuple(outstanding)
            try:
                # Per-item mode: the mapping itself is never run through
                # ``is_valid`` (that predicate judges individual *values*, which
                # are checked per key below). Retries / timeout / breaker still
                # apply to the whole call.
                partial = self._call_with_retries(
                    provider.name,
                    lambda p=provider, r=requested: p.call(r),
                    validate=False,
                )
            except _ProviderError as exc:
                errors[provider.name] = exc.cause
                continue

            served_any = False
            for key in requested:
                if key in partial and self.is_valid(partial[key]):
                    values[key] = partial[key]
                    provenance[key] = provider.name
                    served_any = True
            if served_any:
                outstanding = [k for k in outstanding if k not in values]

        if require_all and outstanding:
            raise AllProvidersFailed(errors)
        if not values and errors:
            # Nothing served and at least one provider blew up: surface why.
            raise AllProvidersFailed(errors)

        return ItemResult(
            values=values,
            provenance=provenance,
            missing=outstanding,
            attempts=attempts,
        )

    # -- internals ---------------------------------------------------------- #
    def _call_with_retries(
        self, name: str, call: Callable[[], T], *, validate: bool = True
    ) -> T:
        """Call ``call`` with retries, timeout, validity, and breaker updates.

        Args:
            name: Provider name (for breaker bookkeeping and error messages).
            call: Zero-argument callable to invoke.
            validate: When True (whole-call mode), apply ``is_valid`` to the
                result. When False (per-item mode), skip it — the predicate
                judges individual item values, not the partial mapping.

        Raises:
            _ProviderError: Wrapping the last failure cause if every attempt
                for this provider failed.
        """
        last_cause: BaseException = RuntimeError("provider produced no result")
        total_attempts = self.retries + 1

        for attempt in range(total_attempts):
            start = self._clock()
            try:
                value = call()
            except Exception as exc:  # noqa: BLE001 - intentional: failover boundary
                last_cause = exc
            else:
                elapsed = self._clock() - start
                if self.timeout is not None and elapsed > self.timeout:
                    last_cause = TimeoutError(
                        f"{name} took {elapsed:.3f}s (timeout {self.timeout:.3f}s)"
                    )
                elif validate and not self.is_valid(value):
                    last_cause = _InvalidResult(
                        f"{name} returned a result rejected by is_valid"
                    )
                else:
                    if self.circuit_breaker is not None:
                        self.circuit_breaker.record_success(name)
                    return value

            # This attempt failed; back off before the next retry of the SAME
            # provider (not before failing over to the next provider).
            if attempt < total_attempts - 1 and self.retry_backoff > 0:
                self._sleep(self.retry_backoff * (attempt + 1))

        if self.circuit_breaker is not None:
            self.circuit_breaker.record_failure(name)
        raise _ProviderError(last_cause)


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #
class _ProviderError(Exception):
    """Internal: a provider exhausted its retries. Carries the root cause."""

    def __init__(self, cause: BaseException) -> None:
        self.cause = cause
        super().__init__(str(cause))


class _InvalidResult(Exception):
    """Internal: a result was rejected by the ``is_valid`` predicate."""
