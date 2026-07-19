from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from uuid import UUID

from evolvable_memory.application.commands import (
    CorrectPreference,
    MemoryUsageResult,
    OutcomeResult,
    PreferenceResult,
    ProjectRecallContext,
    RecallContextResult,
    RecallMemory,
    RecallResult,
    RecordMemoryUsage,
    RecordOutcome,
    RememberPreference,
)
from evolvable_memory.application.governance import (
    EraseSubject,
    IssueProcessingGrant,
    PrivacyApplication,
    RevokeProcessingGrant,
    SuppressProcessing,
)
from evolvable_memory.application.ports import (
    AuthorizationAuditPort,
    AuthorizationPort,
    Clock,
)
from evolvable_memory.application.security import (
    AuthorizationAuditEvent,
    AuthorizationDeniedError,
    AuthorizationRequest,
    InvocationContext,
    MemoryAction,
    ProtectedResource,
)
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.domain.common import Scope
from evolvable_memory.domain.evolution import StrategySnapshot
from evolvable_memory.domain.governance import ErasureRequest, ProcessingGrant, SuppressionFence
from evolvable_memory.domain.memory import MemoryRevision, MemorySnapshot


class AuthorizedMemoryApplication:
    """Application permission enforcement point around the memory use cases."""

    def __init__(
        self,
        *,
        application: MemoryApplication,
        authorization: AuthorizationPort,
        audit: AuthorizationAuditPort,
        clock: Clock,
        privacy: PrivacyApplication | None = None,
    ) -> None:
        self._application = application
        self._authorization = authorization
        self._audit = audit
        self._clock = clock
        self._privacy = privacy

    @property
    def retrieval_policy(self) -> StrategySnapshot:
        return self._application.retrieval_policy

    def is_ready(self) -> bool:
        return self._application.is_ready() and (self._privacy is None or self._privacy.is_ready())

    def close(self) -> None:
        try:
            if self._privacy is not None:
                self._privacy.close()
        finally:
            self._application.close()

    def remember_preference(
        self,
        invocation: InvocationContext,
        command: RememberPreference,
    ) -> PreferenceResult:
        self._authorize(
            invocation,
            action=MemoryAction.EVIDENCE_INGEST,
            scope=command.scope,
        )
        with self._processing(invocation, command.scope):
            return self._application.remember_preference(command)

    def correct_preference(
        self,
        invocation: InvocationContext,
        command: CorrectPreference,
    ) -> PreferenceResult:
        self._authorize(
            invocation,
            action=MemoryAction.BELIEF_CORRECT,
            scope=command.scope,
            resource_id=command.record_id,
        )
        with self._processing(invocation, command.scope):
            return self._application.correct_preference(command)

    def recall(
        self,
        invocation: InvocationContext,
        command: RecallMemory,
    ) -> RecallResult:
        self._authorize(
            invocation,
            action=MemoryAction.PROJECTION_RECALL,
            scope=command.scope,
        )
        with self._processing(invocation, command.scope):
            return self._application.recall(command)

    def record_outcome(
        self,
        invocation: InvocationContext,
        command: RecordOutcome,
    ) -> OutcomeResult:
        self._authorize(
            invocation,
            action=MemoryAction.EXPERIENCE_OUTCOME_WRITE,
            scope=command.scope,
            resource_id=command.trace_id,
        )
        with self._processing(invocation, command.scope):
            return self._application.record_outcome(command)

    def record_usage(
        self,
        invocation: InvocationContext,
        command: RecordMemoryUsage,
    ) -> MemoryUsageResult:
        self._authorize(
            invocation,
            action=MemoryAction.EXPERIENCE_USAGE_WRITE,
            scope=command.scope,
            resource_id=command.trace_id,
        )
        with self._processing(invocation, command.scope):
            return self._application.record_usage(command)

    def project_recall_context(
        self,
        invocation: InvocationContext,
        command: ProjectRecallContext,
    ) -> RecallContextResult:
        self._authorize(
            invocation,
            action=MemoryAction.PROJECTION_COMPRESS,
            scope=command.scope,
            resource_id=command.trace_id,
        )
        with self._processing(invocation, command.scope):
            return self._application.project_recall_context(command)

    def history(
        self,
        invocation: InvocationContext,
        scope: Scope,
        record_id: UUID,
    ) -> tuple[MemoryRevision, ...]:
        self._authorize(
            invocation,
            action=MemoryAction.BELIEF_READ_HISTORY,
            scope=scope,
            resource_id=record_id,
        )
        with self._processing(invocation, scope):
            return self._application.history(scope, record_id)

    def list_preferences(
        self,
        invocation: InvocationContext,
        scope: Scope,
    ) -> tuple[MemorySnapshot, ...]:
        self._authorize(
            invocation,
            action=MemoryAction.BELIEF_READ_CURRENT,
            scope=scope,
        )
        with self._processing(invocation, scope):
            return self._application.list_preferences(scope)

    def issue_processing_grant(
        self,
        invocation: InvocationContext,
        command: IssueProcessingGrant,
    ) -> ProcessingGrant:
        privacy = self._require_privacy()
        self._authorize(
            invocation,
            action=MemoryAction.GOVERNANCE_POLICY_MANAGE,
            scope=command.scope,
        )
        return privacy.issue_processing_grant(command, issued_by=invocation.actor.principal_id)

    def revoke_processing_grant(
        self,
        invocation: InvocationContext,
        command: RevokeProcessingGrant,
    ) -> ProcessingGrant:
        privacy = self._require_privacy()
        self._authorize(
            invocation,
            action=MemoryAction.GOVERNANCE_POLICY_MANAGE,
            scope=command.scope,
            resource_id=command.grant_id,
        )
        return privacy.revoke_processing_grant(command, revoked_by=invocation.actor.principal_id)

    def suppress(
        self,
        invocation: InvocationContext,
        command: SuppressProcessing,
    ) -> SuppressionFence:
        privacy = self._require_privacy()
        self._authorize(
            invocation,
            action=MemoryAction.GOVERNANCE_PRIVACY_SUPPRESS,
            scope=command.scope,
        )
        return privacy.suppress(command, requested_by=invocation.actor.principal_id)

    def erase(
        self,
        invocation: InvocationContext,
        command: EraseSubject,
    ) -> ErasureRequest:
        privacy = self._require_privacy()
        self._authorize(
            invocation,
            action=MemoryAction.GOVERNANCE_ERASURE_APPROVE,
            scope=command.scope,
        )
        return privacy.erase(command, requested_by=invocation.actor.principal_id)

    def erasure(
        self,
        invocation: InvocationContext,
        scope: Scope,
        request_id: UUID,
    ) -> ErasureRequest:
        privacy = self._require_privacy()
        self._authorize(
            invocation,
            action=MemoryAction.GOVERNANCE_ERASURE_APPROVE,
            scope=scope,
            resource_id=request_id,
        )
        return privacy.erasure(scope, request_id)

    def _processing(
        self,
        invocation: InvocationContext,
        scope: Scope,
    ) -> AbstractContextManager[None]:
        if self._privacy is None:
            return nullcontext()
        return self._privacy.governance.processing_context(
            scope,
            purpose=invocation.purpose,
            at=self._clock.now(),
        )

    def _require_privacy(self) -> PrivacyApplication:
        if self._privacy is None:
            raise RuntimeError("privacy governance is not configured")
        return self._privacy

    def _authorize(
        self,
        invocation: InvocationContext,
        *,
        action: MemoryAction,
        scope: Scope,
        resource_id: UUID | None = None,
    ) -> None:
        resource = ProtectedResource(
            scope=scope,
            plane=action.plane,
            resource_id=str(resource_id) if resource_id is not None else None,
        )
        request = AuthorizationRequest(
            actor=invocation.actor,
            action=action,
            resource=resource,
            purpose=invocation.purpose,
        )
        decision = self._authorization.decide(request)
        self._audit.record(
            AuthorizationAuditEvent(
                decision=decision,
                actor=invocation.actor,
                action=action,
                resource=resource,
                purpose=invocation.purpose,
                request_id=invocation.request_id,
                recorded_at=self._clock.now(),
            )
        )
        if not decision.allowed:
            raise AuthorizationDeniedError(conceal_resource=decision.reason == "scope_not_granted")
