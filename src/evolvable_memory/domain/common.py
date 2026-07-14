from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256


class DomainError(ValueError):
    """Base error for violated memory-domain invariants."""


class NotFoundError(DomainError):
    """A scope-local domain object does not exist."""


class ConflictError(DomainError):
    """An operation conflicts with current state or idempotency."""


class AttributionError(DomainError):
    """An outcome cannot be attributed to the supplied recall trace."""


def require_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise DomainError(f"{field} must not be blank")
    return normalized


def require_utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Scope:
    tenant_id: str
    subject_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", require_text(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "subject_id", require_text(self.subject_id, "subject_id"))


@dataclass(frozen=True, slots=True)
class ContextSignature:
    """Canonical, hashable representation of a recall or belief context."""

    facets: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        canonical = tuple(
            sorted(
                (require_text(key, "context key"), require_text(value, "context value"))
                for key, value in self.facets
            )
        )
        if len(dict(canonical)) != len(canonical):
            raise DomainError("context keys must be unique")
        object.__setattr__(self, "facets", canonical)

    @classmethod
    def from_mapping(cls, values: Mapping[str, str] | None) -> ContextSignature:
        return cls(tuple((key, value) for key, value in (values or {}).items()))

    def as_dict(self) -> dict[str, str]:
        return dict(self.facets)

    @property
    def fingerprint(self) -> str:
        encoded = "\x1f".join(f"{key}\x1e{value}" for key, value in self.facets)
        return sha256(encoded.encode("utf-8")).hexdigest()

    def similarity(self, requested: ContextSignature) -> float:
        if not self.facets and not requested.facets:
            return 1.0
        if not self.facets:
            return 0.8
        if not requested.facets:
            return 0.25

        stored = set(self.facets)
        query = set(requested.facets)
        matching = len(stored & query)
        conflicting_keys = {
            stored_key
            for stored_key, stored_value in stored
            for query_key, query_value in query
            if stored_key == query_key and stored_value != query_value
        }
        union = len(stored | query)
        score = matching / union if union else 0.0
        return max(0.0, score - (0.35 * len(conflicting_keys)))


def utc_now() -> datetime:
    return datetime.now(tz=UTC)
