from __future__ import annotations

import re
from datetime import datetime
from math import pow
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
from evolvable_memory.application.ports import Clock, IdGenerator, MemoryStore
from evolvable_memory.domain.common import (
    AttributionError,
    ConflictError,
    ContextSignature,
    DomainError,
    NotFoundError,
    Scope,
)
from evolvable_memory.domain.evidence import (
    Candidate,
    EvidenceSpan,
    EvidenceStance,
    Observation,
    ObservationKind,
)
from evolvable_memory.domain.evolution import RetrievalWeights, StrategySnapshot
from evolvable_memory.domain.experience import (
    OutcomeEvent,
    RecalledItem,
    RecallTrace,
    ScoreBreakdown,
)
from evolvable_memory.domain.memory import (
    BeliefState,
    MemoryKind,
    MemoryRecord,
    MemoryRevision,
    MemorySnapshot,
    RevisionTransition,
    TransitionKind,
)


class MemoryApplication:
    def __init__(
        self,
        *,
        store: MemoryStore,
        clock: Clock,
        ids: IdGenerator,
        retrieval_policy: StrategySnapshot | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._ids = ids
        self._policy = retrieval_policy or StrategySnapshot(
            id=ids.new(),
            version=1,
            weights=RetrievalWeights(),
            min_score=0.20,
            recency_half_life_days=180.0,
            created_at=clock.now(),
        )
        self._store.save_strategy(self._policy)

    @property
    def retrieval_policy(self) -> StrategySnapshot:
        return self._policy

    def is_ready(self) -> bool:
        return self._store.is_ready()

    def close(self) -> None:
        self._store.close()

    def remember_preference(self, command: RememberPreference) -> PreferenceResult:
        with self._store.transaction():
            existing = self._store.observation_by_idempotency(
                command.scope, command.idempotency_key
            )
            if existing is not None:
                return self._existing_preference_result(
                    existing,
                    expected_kind=ObservationKind.MESSAGE,
                    source=command.source,
                    key=command.key,
                    value=command.value,
                    context_fingerprint=command.context.fingerprint,
                    evidence_text=command.evidence_text,
                    confidence=command.confidence,
                )

            now = self._clock.now()
            observation = Observation(
                id=self._ids.new(),
                scope=command.scope,
                kind=ObservationKind.MESSAGE,
                source=command.source,
                content=command.evidence_text,
                idempotency_key=command.idempotency_key,
                occurred_at=command.occurred_at,
                ingested_at=now,
            )
            evidence = EvidenceSpan(
                id=self._ids.new(),
                observation_id=observation.id,
                quote=command.evidence_text,
                stance=EvidenceStance.SUPPORTS,
                end_offset=len(command.evidence_text),
            )
            candidate = Candidate(
                id=self._ids.new(),
                scope=command.scope,
                observation_id=observation.id,
                key=command.key,
                value=command.value,
                context=command.context,
                evidence_ids=(evidence.id,),
                confidence=command.confidence,
                proposed_at=now,
            )
            try:
                self._store.save_ingestion(observation, evidence, candidate)
            except ConflictError:
                concurrent = self._store.observation_by_idempotency(
                    command.scope, command.idempotency_key
                )
                if concurrent is None:
                    raise
                return self._existing_preference_result(
                    concurrent,
                    expected_kind=ObservationKind.MESSAGE,
                    source=command.source,
                    key=command.key,
                    value=command.value,
                    context_fingerprint=command.context.fingerprint,
                    evidence_text=command.evidence_text,
                    confidence=command.confidence,
                )
            result = self._accept_candidate(
                candidate,
                valid_from=command.occurred_at,
                source=command.source,
            )
            return result

    def correct_preference(self, command: CorrectPreference) -> PreferenceResult:
        with self._store.transaction():
            current = self._store.snapshot(command.scope, command.record_id)
            if current is None:
                raise NotFoundError("memory record not found in scope")
            if current.record.kind is not MemoryKind.PREFERENCE:
                raise DomainError("only preference memories can use this correction command")

            existing = self._store.observation_by_idempotency(
                command.scope, command.idempotency_key
            )
            if existing is not None:
                return self._existing_preference_result(
                    existing,
                    expected_kind=ObservationKind.USER_FEEDBACK,
                    source=command.source,
                    key=current.record.key,
                    value=command.value,
                    context_fingerprint=current.record.context.fingerprint,
                    evidence_text=command.evidence_text,
                    confidence=1.0,
                    expected_record_id=command.record_id,
                    correction_reason=command.reason,
                )
            if (
                command.expected_revision_id is not None
                and current.revision.id != command.expected_revision_id
            ):
                raise ConflictError("expected revision is no longer active")

            now = self._clock.now()
            observation = Observation(
                id=self._ids.new(),
                scope=command.scope,
                kind=ObservationKind.USER_FEEDBACK,
                source=command.source,
                content=command.evidence_text,
                idempotency_key=command.idempotency_key,
                occurred_at=command.occurred_at,
                ingested_at=now,
                metadata=(("correction_reason", command.reason),),
            )
            evidence = EvidenceSpan(
                id=self._ids.new(),
                observation_id=observation.id,
                quote=command.evidence_text,
                # The span supports the new candidate; the revision transition records
                # that the previously active belief was superseded.
                stance=EvidenceStance.SUPPORTS,
                end_offset=len(command.evidence_text),
            )
            candidate = Candidate(
                id=self._ids.new(),
                scope=command.scope,
                observation_id=observation.id,
                key=current.record.key,
                value=command.value,
                context=current.record.context,
                evidence_ids=(evidence.id,),
                confidence=1.0,
                proposed_at=now,
            )
            try:
                self._store.save_ingestion(observation, evidence, candidate)
            except ConflictError:
                concurrent = self._store.observation_by_idempotency(
                    command.scope, command.idempotency_key
                )
                if concurrent is None:
                    raise
                return self._existing_preference_result(
                    concurrent,
                    expected_kind=ObservationKind.USER_FEEDBACK,
                    source=command.source,
                    key=current.record.key,
                    value=command.value,
                    context_fingerprint=current.record.context.fingerprint,
                    evidence_text=command.evidence_text,
                    confidence=1.0,
                    expected_record_id=command.record_id,
                    correction_reason=command.reason,
                )
            return self._accept_candidate(
                candidate,
                valid_from=command.occurred_at,
                source=command.source,
                reason=command.reason,
                expected_record_id=command.record_id,
            )

    def recall(self, command: RecallMemory) -> RecallResult:
        if not 1 <= command.limit <= 100:
            raise DomainError("recall limit must be in [1, 100]")
        query = command.query.strip()
        if not query:
            raise DomainError("recall query must not be blank")

        now = self._clock.now()
        valid_at = command.valid_at or now
        known_at = command.known_at or now
        if known_at > now:
            raise DomainError("known_at must not be in the future")

        scored: list[tuple[float, datetime, MemoryRecord, MemoryRevision, ScoreBreakdown]] = []
        snapshots = self._store.memories_as_of(
            command.scope,
            valid_at=valid_at,
            known_at=known_at,
        )
        for snapshot in snapshots:
            record = snapshot.record
            revision = snapshot.revision
            semantic = _lexical_similarity(query, f"{record.key} {revision.value}")
            context = record.context.similarity(command.context)
            belief = revision.belief.confidence
            utility = self._store.utility_for_as_of(
                command.scope,
                revision.id,
                command.context,
                known_at=known_at,
            ).mean
            age_days = max(
                0.0,
                (valid_at - revision.belief.last_evidence_at).total_seconds() / 86_400.0,
            )
            recency = pow(0.5, age_days / self._policy.recency_half_life_days)
            breakdown = ScoreBreakdown(
                semantic=semantic,
                context=context,
                belief=belief,
                utility=utility,
                recency=recency,
            )
            if not _passes_relevance_admission(
                semantic=semantic,
                stored_context=record.context,
                requested_context=command.context,
                context_score=context,
            ):
                continue
            score = _weighted_score(breakdown, self._policy.weights)
            if score >= self._policy.min_score:
                scored.append((score, revision.recorded_at, record, revision, breakdown))

        scored.sort(key=lambda entry: (entry[0], entry[1], str(entry[3].id)), reverse=True)
        items = tuple(
            RecalledItem(
                record_id=record.id,
                revision_id=revision.id,
                key=record.key,
                value=revision.value,
                context=record.context,
                revision_valid_from=revision.valid_from,
                revision_recorded_at=revision.recorded_at,
                rank=rank,
                score=score,
                breakdown=breakdown,
                evidence_ids=revision.evidence_ids,
            )
            for rank, (score, _, record, revision, breakdown) in enumerate(
                scored[: command.limit], start=1
            )
        )
        trace = RecallTrace(
            id=self._ids.new(),
            scope=command.scope,
            query=query,
            context=command.context,
            policy_id=self._policy.id,
            policy_version=self._policy.version,
            items=items,
            valid_at=valid_at,
            known_at=known_at,
            created_at=now,
        )
        self._store.save_trace(trace)
        return trace

    def record_outcome(self, command: RecordOutcome) -> OutcomeResult:
        with self._store.transaction():
            trace = self._store.trace(command.scope, command.trace_id)
            if trace is None:
                raise NotFoundError("recall trace not found in scope")
            if not any(item.revision_id == command.revision_id for item in trace.items):
                raise AttributionError("revision was not present in the supplied recall trace")

            outcome = OutcomeEvent(
                id=self._ids.new(),
                scope=command.scope,
                trace_id=command.trace_id,
                revision_id=command.revision_id,
                kind=command.kind,
                idempotency_key=command.idempotency_key,
                occurred_at=command.occurred_at,
                recorded_at=self._clock.now(),
                weight=command.weight,
                note=command.note,
            )
            stored, utility, created = self._store.apply_outcome(outcome, trace.context)
            if not created and stored.note != outcome.note:
                raise ConflictError("outcome idempotency key was reused with different data")
            return OutcomeResult(
                outcome=stored,
                utility=utility,
                idempotent_replay=not created,
            )

    def history(self, scope: Scope, record_id: UUID) -> tuple[MemoryRevision, ...]:
        if self._store.snapshot(scope, record_id) is None:
            raise NotFoundError("memory record not found in scope")
        return self._store.revision_history(scope, record_id)

    def list_preferences(self, scope: Scope) -> tuple[MemorySnapshot, ...]:
        """Return current preference heads in a stable, scope-local order."""
        now = self._clock.now()
        snapshots = self._store.memories_as_of(scope, valid_at=now, known_at=now)
        preferences = (
            snapshot for snapshot in snapshots if snapshot.record.kind is MemoryKind.PREFERENCE
        )
        return tuple(
            sorted(
                preferences,
                key=lambda snapshot: (
                    snapshot.record.key,
                    snapshot.record.context.fingerprint,
                    str(snapshot.record.id),
                ),
            )
        )

    def _accept_candidate(
        self,
        candidate: Candidate,
        *,
        valid_from: datetime,
        source: str,
        reason: str | None = None,
        expected_record_id: UUID | None = None,
    ) -> PreferenceResult:
        now = self._clock.now()
        current = self._store.current_by_identity(candidate.scope, candidate.key, candidate.context)
        if current is None:
            if expected_record_id is not None:
                raise ConflictError("expected memory record is no longer active")
            record = MemoryRecord(
                id=self._ids.new(),
                scope=candidate.scope,
                kind=MemoryKind.PREFERENCE,
                key=candidate.key,
                context=candidate.context,
                created_at=now,
            )
            revision = MemoryRevision(
                id=self._ids.new(),
                record_id=record.id,
                sequence=1,
                value=candidate.value,
                belief=BeliefState(
                    confidence=candidate.confidence,
                    support_count=1,
                    contradiction_count=0,
                    source_diversity=1,
                    last_evidence_at=valid_from,
                    source_keys=(source,),
                ),
                evidence_ids=candidate.evidence_ids,
                valid_from=valid_from,
                recorded_at=now,
            )
            transition = RevisionTransition(
                id=self._ids.new(),
                record_id=record.id,
                kind=TransitionKind.CREATED,
                occurred_at=now,
                to_revision_id=revision.id,
                reason=reason,
            )
            self._store.add_memory(record, revision, transition)
        else:
            if expected_record_id is not None and current.record.id != expected_record_id:
                raise ConflictError("correction target does not match active memory identity")
            if current.revision.value == candidate.value:
                belief = current.revision.belief.reinforced(
                    candidate.confidence,
                    valid_from,
                    source=source,
                )
                evidence_ids = current.revision.evidence_ids + candidate.evidence_ids
            else:
                belief = BeliefState(
                    confidence=candidate.confidence,
                    support_count=1,
                    contradiction_count=0,
                    source_diversity=1,
                    last_evidence_at=valid_from,
                    source_keys=(source,),
                )
                evidence_ids = candidate.evidence_ids
            record = current.record
            revision = MemoryRevision(
                id=self._ids.new(),
                record_id=record.id,
                sequence=current.revision.sequence + 1,
                value=candidate.value,
                belief=belief,
                evidence_ids=evidence_ids,
                valid_from=valid_from,
                recorded_at=now,
                supersedes_revision_id=current.revision.id,
            )
            transition = RevisionTransition(
                id=self._ids.new(),
                record_id=record.id,
                kind=TransitionKind.SUPERSEDED,
                occurred_at=now,
                from_revision_id=current.revision.id,
                to_revision_id=revision.id,
                reason=reason,
            )
            self._store.append_revision(
                expected_revision_id=current.revision.id,
                revision=revision,
                transition=transition,
            )

        self._store.update_candidate(candidate.accept(record.id, revision.id))
        return PreferenceResult(
            observation_id=candidate.observation_id,
            candidate_id=candidate.id,
            record_id=record.id,
            revision_id=revision.id,
            sequence=revision.sequence,
            idempotent_replay=False,
        )

    def _existing_preference_result(
        self,
        observation: Observation,
        *,
        expected_kind: ObservationKind,
        source: str,
        key: str,
        value: str,
        context_fingerprint: str,
        evidence_text: str,
        confidence: float,
        expected_record_id: UUID | None = None,
        correction_reason: str | None = None,
    ) -> PreferenceResult:
        candidate = self._store.candidate_for_observation(observation.scope, observation.id)
        if (
            candidate is None
            or candidate.accepted_record_id is None
            or candidate.accepted_revision_id is None
        ):
            raise ConflictError("idempotency key belongs to an incomplete ingestion")
        # occurred_at is intentionally excluded: the HTTP boundary supplies the current
        # time when callers omit it, so a safe network retry may carry a different value.
        # The stable, caller-controlled business fields below form the replay identity.
        if (
            observation.kind is not expected_kind
            or observation.source != source
            or observation.content != evidence_text
            or candidate.key != key
            or candidate.value != value
            or candidate.context.fingerprint != context_fingerprint
            or candidate.confidence != confidence
            or (
                expected_record_id is not None
                and candidate.accepted_record_id != expected_record_id
            )
            or dict(observation.metadata).get("correction_reason") != correction_reason
        ):
            raise ConflictError("idempotency key was reused for a different preference request")
        snapshot = self._store.snapshot(candidate.scope, candidate.accepted_record_id)
        history = self._store.revision_history(candidate.scope, candidate.accepted_record_id)
        matching = next(
            (revision for revision in history if revision.id == candidate.accepted_revision_id),
            None,
        )
        if snapshot is None or matching is None:
            raise ConflictError("accepted idempotent result is no longer resolvable")
        return PreferenceResult(
            observation_id=observation.id,
            candidate_id=candidate.id,
            record_id=candidate.accepted_record_id,
            revision_id=candidate.accepted_revision_id,
            sequence=matching.sequence,
            idempotent_replay=True,
        )


_ASCII_WORD = re.compile(r"[a-z0-9]+", flags=re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    lowered = text.casefold()
    tokens = set(_ASCII_WORD.findall(lowered))
    cjk = [char for char in lowered if "\u3400" <= char <= "\u9fff"]
    tokens.update(cjk)
    tokens.update("".join(cjk[index : index + 2]) for index in range(len(cjk) - 1))
    return tokens


def _lexical_similarity(query: str, document: str) -> float:
    query_tokens = _tokens(query)
    document_tokens = _tokens(document)
    if not query_tokens or not document_tokens:
        return 0.0
    return len(query_tokens & document_tokens) / len(query_tokens | document_tokens)


def _passes_relevance_admission(
    *,
    semantic: float,
    stored_context: ContextSignature,
    requested_context: ContextSignature,
    context_score: float,
) -> bool:
    """Keep belief strength and freshness from making an unrelated item relevant.

    Explicit context is allowed to bridge vocabulary differences (for example a
    Chinese query recalling an English-valued preference), but an absent context is
    not itself evidence of relevance. This admission rule is deliberately outside
    the evolvable weight snapshot: strategy tuning must not remove the safety floor.
    """
    if semantic > 0.0:
        return True
    return bool(stored_context.facets and requested_context.facets) and context_score > 0.0


def _weighted_score(breakdown: ScoreBreakdown, weights: RetrievalWeights) -> float:
    return min(
        1.0,
        (breakdown.semantic * weights.semantic)
        + (breakdown.context * weights.context)
        + (breakdown.belief * weights.belief)
        + (breakdown.utility * weights.utility)
        + (breakdown.recency * weights.recency),
    )
