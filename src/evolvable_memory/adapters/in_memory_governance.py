from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from threading import RLock
from uuid import UUID

from evolvable_memory.domain.common import ConflictError, NotFoundError, Scope
from evolvable_memory.domain.governance import (
    ErasureRequest,
    ErasureStatus,
    ErasureSummary,
    ProcessingDeniedError,
    ProcessingGrant,
    SuppressionFence,
)


class InMemoryPrivacyGovernance:
    """Executable local governance adapter; never qualifies as production authority."""

    def __init__(self, hmac_key: bytes = b"development-governance-key-change-me") -> None:
        if len(hmac_key) < 16:
            raise ValueError("governance HMAC key must contain at least 16 bytes")
        self._hmac_key = hmac_key
        self._lock = RLock()
        self._grants: dict[UUID, ProcessingGrant] = {}
        self._grant_keys: dict[tuple[str, str, str], UUID] = {}
        self._fences: dict[tuple[str, str], SuppressionFence] = {}
        self._erasures: dict[UUID, ErasureRequest] = {}
        self._erasure_keys: dict[tuple[str, str, str], UUID] = {}

    def close(self) -> None:
        return

    def is_ready(self) -> bool:
        return True

    @contextmanager
    def processing_context(
        self,
        scope: Scope,
        *,
        purpose: str,
        at: datetime,
    ) -> Iterator[None]:
        refs = self._scope_refs(scope)
        with self._lock:
            if refs in self._fences:
                raise ProcessingDeniedError("processing_suppressed")
            if not any(
                grant.tenant_ref == refs[0]
                and grant.subject_ref == refs[1]
                and purpose in grant.purposes
                and grant.revoked_at is None
                and grant.valid_from <= at
                and (grant.valid_until is None or at < grant.valid_until)
                for grant in self._grants.values()
            ):
                raise ProcessingDeniedError("processing_not_granted")
            yield

    @contextmanager
    def projection_context(self, scope: Scope, *, at: datetime) -> Iterator[None]:
        del at
        refs = self._scope_refs(scope)
        with self._lock:
            if refs in self._fences:
                raise ProcessingDeniedError("processing_suppressed")
            yield

    def issue_processing_grant(
        self,
        *,
        grant_id: UUID,
        scope: Scope,
        purposes: tuple[str, ...],
        lawful_basis: str,
        policy_version: str,
        issued_by: str,
        idempotency_key: str,
        valid_from: datetime,
        valid_until: datetime | None,
        created_at: datetime,
    ) -> ProcessingGrant:
        tenant_ref, subject_ref = self._scope_refs(scope)
        grant = ProcessingGrant(
            id=grant_id,
            tenant_ref=tenant_ref,
            subject_ref=subject_ref,
            purposes=purposes,
            lawful_basis=lawful_basis,
            policy_version=policy_version,
            issued_by_ref=self._reference("principal", issued_by),
            idempotency_key=idempotency_key,
            valid_from=valid_from,
            valid_until=valid_until,
            created_at=created_at,
        )
        key = (tenant_ref, subject_ref, grant.idempotency_key)
        with self._lock:
            existing_id = self._grant_keys.get(key)
            if existing_id is not None:
                existing = self._grants[existing_id]
                if _grant_business_values(existing) != _grant_business_values(grant):
                    raise ConflictError(
                        "processing grant idempotency key was reused with different data"
                    )
                return existing
            self._grants[grant.id] = grant
            self._grant_keys[key] = grant.id
            return grant

    def revoke_processing_grant(
        self,
        *,
        scope: Scope,
        grant_id: UUID,
        revoked_by: str,
        revoked_at: datetime,
    ) -> ProcessingGrant:
        refs = self._scope_refs(scope)
        with self._lock:
            current = self._grants.get(grant_id)
            if current is None or (current.tenant_ref, current.subject_ref) != refs:
                raise NotFoundError("processing grant not found in scope")
            if current.revoked_at is not None:
                return current
            revoked = replace(
                current,
                revoked_at=revoked_at,
                revoked_by_ref=self._reference("principal", revoked_by),
            )
            self._grants[grant_id] = revoked
            return revoked

    def suppress(
        self,
        *,
        fence_id: UUID,
        scope: Scope,
        reason_code: str,
        policy_version: str,
        requested_by: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> SuppressionFence:
        refs = self._scope_refs(scope)
        fence = SuppressionFence(
            id=fence_id,
            tenant_ref=refs[0],
            subject_ref=refs[1],
            reason_code=reason_code,
            policy_version=policy_version,
            requested_by_ref=self._reference("principal", requested_by),
            idempotency_key=idempotency_key,
            created_at=created_at,
        )
        with self._lock:
            existing = self._fences.get(refs)
            if existing is not None:
                return existing
            self._fences[refs] = fence
            return fence

    def begin_erasure(
        self,
        *,
        request_id: UUID,
        fence_id: UUID,
        scope: Scope,
        reason_code: str,
        policy_version: str,
        requested_by: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> ErasureRequest:
        refs = self._scope_refs(scope)
        key = (*refs, idempotency_key)
        with self._lock:
            existing_id = self._erasure_keys.get(key)
            if existing_id is not None:
                existing = self._erasures[existing_id]
                if existing.reason_code != reason_code or existing.policy_version != policy_version:
                    raise ConflictError("erasure idempotency key was reused with different data")
                return existing
            self.suppress(
                fence_id=fence_id,
                scope=scope,
                reason_code=reason_code,
                policy_version=policy_version,
                requested_by=requested_by,
                idempotency_key=f"erasure:{idempotency_key}",
                created_at=created_at,
            )
            request = ErasureRequest(
                id=request_id,
                tenant_ref=refs[0],
                subject_ref=refs[1],
                scope_digest=self._scope_digest(scope),
                reason_code=reason_code,
                policy_version=policy_version,
                requested_by_ref=self._reference("principal", requested_by),
                idempotency_key=idempotency_key,
                status=ErasureStatus.PENDING,
                created_at=created_at,
            )
            self._erasures[request.id] = request
            self._erasure_keys[key] = request.id
            return request

    def complete_erasure(
        self,
        *,
        scope: Scope,
        request_id: UUID,
        summary: ErasureSummary,
        handler_results: tuple[tuple[str, str], ...],
        completed_at: datetime,
    ) -> ErasureRequest:
        with self._lock:
            current = self._erasure_in_scope(scope, request_id)
            if current.status is ErasureStatus.COMPLETED:
                return current
            completed = replace(
                current,
                status=ErasureStatus.COMPLETED,
                completed_at=completed_at,
                summary=summary,
                handler_results=handler_results,
                error_code=None,
            )
            self._erasures[request_id] = completed
            return completed

    def fail_erasure(
        self,
        *,
        scope: Scope,
        request_id: UUID,
        error_code: str,
    ) -> ErasureRequest:
        with self._lock:
            current = self._erasure_in_scope(scope, request_id)
            if current.status is ErasureStatus.COMPLETED:
                return current
            pending = replace(
                current,
                status=ErasureStatus.PENDING,
                error_code=error_code,
            )
            self._erasures[request_id] = pending
            return pending

    def erasure(self, scope: Scope, request_id: UUID) -> ErasureRequest | None:
        with self._lock:
            request = self._erasures.get(request_id)
            return (
                request
                if request is not None
                and (request.tenant_ref, request.subject_ref) == self._scope_refs(scope)
                else None
            )

    def _erasure_in_scope(self, scope: Scope, request_id: UUID) -> ErasureRequest:
        request = self.erasure(scope, request_id)
        if request is None:
            raise NotFoundError("erasure request not found in scope")
        return request

    def _scope_refs(self, scope: Scope) -> tuple[str, str]:
        return (
            self._reference("tenant", scope.tenant_id),
            self._reference("subject", f"{scope.tenant_id}\0{scope.subject_id}"),
        )

    def _scope_digest(self, scope: Scope) -> str:
        return self._reference("scope", f"{scope.tenant_id}\0{scope.subject_id}")

    def _reference(self, kind: str, value: str) -> str:
        return hmac.new(
            self._hmac_key,
            f"{kind}:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()


class DevelopmentBypassPrivacyGovernance(InMemoryPrivacyGovernance):
    """Explicit local-only adapter that preserves the public development sandbox."""

    @contextmanager
    def processing_context(
        self,
        scope: Scope,
        *,
        purpose: str,
        at: datetime,
    ) -> Iterator[None]:
        del purpose, at
        refs = self._scope_refs(scope)
        with self._lock:
            if refs in self._fences:
                raise ProcessingDeniedError("processing_suppressed")
            yield

    @contextmanager
    def projection_context(self, scope: Scope, *, at: datetime) -> Iterator[None]:
        del at
        refs = self._scope_refs(scope)
        with self._lock:
            if refs in self._fences:
                raise ProcessingDeniedError("processing_suppressed")
            yield


def _grant_business_values(grant: ProcessingGrant) -> tuple[object, ...]:
    return (
        grant.tenant_ref,
        grant.subject_ref,
        grant.purposes,
        grant.lawful_basis,
        grant.policy_version,
        grant.valid_from,
        grant.valid_until,
    )
