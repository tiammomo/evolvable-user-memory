from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from evolvable_memory.application.ports import (
    Clock,
    IdGenerator,
    PrivacyGovernancePort,
)
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.domain.common import (
    DomainError,
    NotFoundError,
    Scope,
    require_text,
    require_utc,
)
from evolvable_memory.domain.governance import (
    ErasureRequest,
    ErasureStatus,
    GovernanceUnavailableError,
    ProcessingGrant,
    SuppressionFence,
)


@dataclass(frozen=True, slots=True)
class IssueProcessingGrant:
    scope: Scope
    purposes: tuple[str, ...]
    lawful_basis: str
    idempotency_key: str
    valid_from: datetime
    valid_until: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "lawful_basis", require_text(self.lawful_basis, "lawful_basis"))
        object.__setattr__(
            self,
            "idempotency_key",
            require_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "valid_from", require_utc(self.valid_from, "valid_from"))
        purposes = tuple(sorted({require_text(value, "purposes") for value in self.purposes}))
        if not purposes or "*" in purposes:
            raise DomainError("processing grant purposes must be explicit")
        object.__setattr__(self, "purposes", purposes)
        if self.valid_until is not None:
            valid_until = require_utc(self.valid_until, "valid_until")
            if valid_until <= self.valid_from:
                raise DomainError("valid_until must be later than valid_from")
            object.__setattr__(
                self,
                "valid_until",
                valid_until,
            )


@dataclass(frozen=True, slots=True)
class RevokeProcessingGrant:
    scope: Scope
    grant_id: UUID


@dataclass(frozen=True, slots=True)
class SuppressProcessing:
    scope: Scope
    reason_code: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_code", require_text(self.reason_code, "reason_code"))
        object.__setattr__(
            self,
            "idempotency_key",
            require_text(self.idempotency_key, "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class EraseSubject:
    scope: Scope
    reason_code: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_code", require_text(self.reason_code, "reason_code"))
        object.__setattr__(
            self,
            "idempotency_key",
            require_text(self.idempotency_key, "idempotency_key"),
        )


class PrivacyApplication:
    """Fixed governance workflow; strategy evolution cannot reach this control plane."""

    def __init__(
        self,
        *,
        governance: PrivacyGovernancePort,
        memory: MemoryApplication,
        clock: Clock,
        ids: IdGenerator,
        policy_version: str,
    ) -> None:
        self._governance = governance
        self._memory = memory
        self._clock = clock
        self._ids = ids
        self._policy_version = require_text(policy_version, "policy_version")

    @property
    def governance(self) -> PrivacyGovernancePort:
        return self._governance

    def is_ready(self) -> bool:
        return self._governance.is_ready()

    def close(self) -> None:
        self._governance.close()

    def issue_processing_grant(
        self,
        command: IssueProcessingGrant,
        *,
        issued_by: str,
    ) -> ProcessingGrant:
        now = self._clock.now()
        return self._governance.issue_processing_grant(
            grant_id=self._ids.new(),
            scope=command.scope,
            purposes=command.purposes,
            lawful_basis=command.lawful_basis,
            policy_version=self._policy_version,
            issued_by=issued_by,
            idempotency_key=command.idempotency_key,
            valid_from=command.valid_from,
            valid_until=command.valid_until,
            created_at=now,
        )

    def revoke_processing_grant(
        self,
        command: RevokeProcessingGrant,
        *,
        revoked_by: str,
    ) -> ProcessingGrant:
        return self._governance.revoke_processing_grant(
            scope=command.scope,
            grant_id=command.grant_id,
            revoked_by=revoked_by,
            revoked_at=self._clock.now(),
        )

    def suppress(
        self,
        command: SuppressProcessing,
        *,
        requested_by: str,
    ) -> SuppressionFence:
        return self._governance.suppress(
            fence_id=self._ids.new(),
            scope=command.scope,
            reason_code=command.reason_code,
            policy_version=self._policy_version,
            requested_by=requested_by,
            idempotency_key=command.idempotency_key,
            created_at=self._clock.now(),
        )

    def erase(
        self,
        command: EraseSubject,
        *,
        requested_by: str,
    ) -> ErasureRequest:
        now = self._clock.now()
        request = self._governance.begin_erasure(
            request_id=self._ids.new(),
            fence_id=self._ids.new(),
            scope=command.scope,
            reason_code=command.reason_code,
            policy_version=self._policy_version,
            requested_by=requested_by,
            idempotency_key=command.idempotency_key,
            created_at=now,
        )
        if request.status is ErasureStatus.COMPLETED:
            return request
        try:
            summary = self._memory.erase_scope(command.scope)
            projection_result = (
                "not_applicable" if self._memory.projection_status == "disabled" else "completed"
            )
            return self._governance.complete_erasure(
                scope=command.scope,
                request_id=request.id,
                summary=summary,
                handler_results=(
                    ("authority", "completed"),
                    ("outbox", "completed"),
                    ("projection", projection_result),
                ),
                completed_at=self._clock.now(),
            )
        except Exception as exc:
            self._governance.fail_erasure(
                scope=command.scope,
                request_id=request.id,
                error_code=type(exc).__name__,
            )
            raise GovernanceUnavailableError(
                "erasure remains pending because a required handler failed"
            ) from exc

    def erasure(self, scope: Scope, request_id: UUID) -> ErasureRequest:
        request = self._governance.erasure(scope, request_id)
        if request is None:
            raise NotFoundError("erasure request not found in scope")
        return request
