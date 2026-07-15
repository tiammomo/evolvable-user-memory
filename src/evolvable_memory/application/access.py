from __future__ import annotations

from uuid import UUID

from evolvable_memory.application.commands import (
    CorrectPreference,
    OutcomeResult,
    PreferenceResult,
    RecallMemory,
    RecallResult,
    RecordOutcome,
    RememberPreference,
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
    ) -> None:
        self._application = application
        self._authorization = authorization
        self._audit = audit
        self._clock = clock

    @property
    def retrieval_policy(self) -> StrategySnapshot:
        return self._application.retrieval_policy

    def is_ready(self) -> bool:
        return self._application.is_ready()

    def close(self) -> None:
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
        return self._application.record_outcome(command)

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
        return self._application.list_preferences(scope)

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
