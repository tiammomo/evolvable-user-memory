from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from threading import local
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg import Error as PsycopgError
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from evolvable_memory.domain.common import ConflictError, ContextSignature, Scope
from evolvable_memory.domain.evidence import (
    Candidate,
    CandidateState,
    EvidenceSpan,
    Observation,
    ObservationKind,
)
from evolvable_memory.domain.evolution import (
    EvolutionExperiment,
    ExperimentStage,
    ExperimentTransition,
    RetrievalWeights,
    StrategyActivation,
    StrategyActivationKind,
    StrategySnapshot,
)
from evolvable_memory.domain.experience import (
    OutcomeEvent,
    OutcomeKind,
    RecalledItem,
    RecallTrace,
    ScoreBreakdown,
    UtilityEstimate,
)
from evolvable_memory.domain.memory import (
    BeliefState,
    MemoryKind,
    MemoryRecord,
    MemoryRevision,
    MemorySnapshot,
    RevisionTransition,
)

DbRow = dict[str, Any]
DbConnection = Connection[DbRow]
_STRATEGY_ACTIVATION_LOCK_ID = 0x45564F4C5645


class _ConnectionState(local):
    connection: DbConnection | None = None


class PostgresMemoryStore:
    """PostgreSQL authority with real transactions and scope-enforced queries."""

    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 10,
        open_timeout: float = 10.0,
        readiness_timeout: float = 1.0,
    ) -> None:
        if readiness_timeout <= 0:
            raise ValueError("readiness_timeout must be positive")
        conninfo = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._pool: ConnectionPool[DbConnection] = ConnectionPool(
            conninfo,
            kwargs={"row_factory": dict_row},
            min_size=min_size,
            max_size=max_size,
            open=True,
            check=ConnectionPool.check_connection,
        )
        self._pool.wait(timeout=open_timeout)
        self._readiness_timeout = readiness_timeout
        self._state = _ConnectionState()

    def close(self) -> None:
        self._pool.close()

    def is_ready(self) -> bool:
        try:
            with self._pool.connection(timeout=self._readiness_timeout) as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except (OSError, PoolClosed, PoolTimeout, PsycopgError):
            return False

    @contextmanager
    def transaction(self) -> Iterator[None]:
        current = self._state.connection
        if current is not None:
            with current.transaction():
                yield
            return

        with self._pool.connection() as connection:
            self._state.connection = connection
            try:
                with connection.transaction():
                    yield
            finally:
                self._state.connection = None

    @contextmanager
    def _connection(self) -> Iterator[DbConnection]:
        current = self._state.connection
        if current is not None:
            yield current
            return
        with self._pool.connection() as connection:
            yield connection

    def observation_by_idempotency(
        self,
        scope: Scope,
        idempotency_key: str,
    ) -> Observation | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM observations
                WHERE tenant_id = %s AND subject_id = %s AND idempotency_key = %s
                """,
                (scope.tenant_id, scope.subject_id, idempotency_key.strip()),
            ).fetchone()
        return _observation(row) if row is not None else None

    def save_ingestion(
        self,
        observation: Observation,
        evidence: EvidenceSpan,
        candidate: Candidate,
    ) -> None:
        try:
            with self._connection() as connection, connection.transaction():
                connection.execute(
                    """
                    INSERT INTO observations (
                        id, tenant_id, subject_id, kind, source, content,
                        idempotency_key, occurred_at, ingested_at, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        observation.id,
                        observation.scope.tenant_id,
                        observation.scope.subject_id,
                        observation.kind.value,
                        observation.source,
                        observation.content,
                        observation.idempotency_key,
                        observation.occurred_at,
                        observation.ingested_at,
                        Jsonb([list(item) for item in observation.metadata]),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO evidence_spans (
                        id, observation_id, quote, stance, start_offset, end_offset
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        evidence.id,
                        evidence.observation_id,
                        evidence.quote,
                        evidence.stance.value,
                        evidence.start_offset,
                        evidence.end_offset,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO candidates (
                        id, tenant_id, subject_id, observation_id, key, value,
                        context, context_fingerprint, evidence_ids, confidence,
                        proposed_at, state, accepted_record_id, accepted_revision_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        candidate.id,
                        candidate.scope.tenant_id,
                        candidate.scope.subject_id,
                        candidate.observation_id,
                        candidate.key,
                        candidate.value,
                        Jsonb(candidate.context.as_dict()),
                        candidate.context.fingerprint,
                        list(candidate.evidence_ids),
                        candidate.confidence,
                        candidate.proposed_at,
                        candidate.state.value,
                        candidate.accepted_record_id,
                        candidate.accepted_revision_id,
                    ),
                )
                self._append_outbox(
                    connection,
                    aggregate_type="observation",
                    aggregate_id=observation.id,
                    event_type="observation.ingested",
                    occurred_at=observation.ingested_at,
                    payload={
                        "tenant_id": observation.scope.tenant_id,
                        "subject_id": observation.scope.subject_id,
                        "candidate_id": str(candidate.id),
                    },
                )
        except (UniqueViolation, ForeignKeyViolation, CheckViolation) as exc:
            raise ConflictError("ingestion conflicts with authoritative state") from exc

    def candidate_for_observation(
        self,
        scope: Scope,
        observation_id: UUID,
    ) -> Candidate | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM candidates
                WHERE tenant_id = %s AND subject_id = %s AND observation_id = %s
                """,
                (scope.tenant_id, scope.subject_id, observation_id),
            ).fetchone()
        return _candidate(row) if row is not None else None

    def update_candidate(self, candidate: Candidate) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE candidates
                SET state = %s, accepted_record_id = %s, accepted_revision_id = %s
                WHERE id = %s AND tenant_id = %s AND subject_id = %s
                """,
                (
                    candidate.state.value,
                    candidate.accepted_record_id,
                    candidate.accepted_revision_id,
                    candidate.id,
                    candidate.scope.tenant_id,
                    candidate.scope.subject_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("candidate does not exist in scope")

    def current_by_identity(
        self,
        scope: Scope,
        key: str,
        context: ContextSignature,
    ) -> MemorySnapshot | None:
        with self._connection() as connection:
            row = connection.execute(
                _SNAPSHOT_SELECT
                + """
                WHERE r.tenant_id = %s AND r.subject_id = %s
                  AND r.key = %s AND r.context_fingerprint = %s
                """,
                (scope.tenant_id, scope.subject_id, key.strip(), context.fingerprint),
            ).fetchone()
        return _snapshot(row) if row is not None else None

    def snapshot(self, scope: Scope, record_id: UUID) -> MemorySnapshot | None:
        with self._connection() as connection:
            row = connection.execute(
                _SNAPSHOT_SELECT
                + """
                WHERE r.tenant_id = %s AND r.subject_id = %s AND r.id = %s
                """,
                (scope.tenant_id, scope.subject_id, record_id),
            ).fetchone()
        return _snapshot(row) if row is not None else None

    def add_memory(
        self,
        record: MemoryRecord,
        revision: MemoryRevision,
        transition: RevisionTransition,
    ) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO memory_records (
                        id, tenant_id, subject_id, kind, key, context,
                        context_fingerprint, created_at, active_revision_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL)
                    """,
                    (
                        record.id,
                        record.scope.tenant_id,
                        record.scope.subject_id,
                        record.kind.value,
                        record.key,
                        Jsonb(record.context.as_dict()),
                        record.context.fingerprint,
                        record.created_at,
                    ),
                )
                self._insert_revision(connection, record.scope, revision)
                self._insert_transition(connection, record.scope, transition)
                connection.execute(
                    """
                    UPDATE memory_records SET active_revision_id = %s
                    WHERE id = %s AND tenant_id = %s AND subject_id = %s
                    """,
                    (
                        revision.id,
                        record.id,
                        record.scope.tenant_id,
                        record.scope.subject_id,
                    ),
                )
                self._revision_outbox(connection, record.scope, revision, "memory.revision.created")
        except (UniqueViolation, ForeignKeyViolation, CheckViolation) as exc:
            raise ConflictError("memory identity or initial revision conflicts") from exc

    def append_revision(
        self,
        *,
        expected_revision_id: UUID,
        revision: MemoryRevision,
        transition: RevisionTransition,
    ) -> None:
        try:
            with self._connection() as connection:
                record_row = connection.execute(
                    """
                    SELECT tenant_id, subject_id, active_revision_id
                    FROM memory_records WHERE id = %s FOR UPDATE
                    """,
                    (revision.record_id,),
                ).fetchone()
                if record_row is None or record_row["active_revision_id"] != expected_revision_id:
                    raise ConflictError("active revision changed concurrently")
                scope = Scope(record_row["tenant_id"], record_row["subject_id"])
                current_row = connection.execute(
                    "SELECT sequence FROM memory_revisions WHERE id = %s",
                    (expected_revision_id,),
                ).fetchone()
                if current_row is None or revision.sequence != current_row["sequence"] + 1:
                    raise ConflictError("revision sequence is not contiguous")
                self._insert_revision(connection, scope, revision)
                self._insert_transition(connection, scope, transition)
                cursor = connection.execute(
                    """
                    UPDATE memory_records SET active_revision_id = %s
                    WHERE id = %s AND active_revision_id = %s
                    """,
                    (revision.id, revision.record_id, expected_revision_id),
                )
                if cursor.rowcount != 1:
                    raise ConflictError("active revision changed concurrently")
                self._revision_outbox(connection, scope, revision, "memory.revision.appended")
        except (UniqueViolation, ForeignKeyViolation, CheckViolation) as exc:
            raise ConflictError("revision conflicts with authoritative state") from exc

    def active_memories(self, scope: Scope) -> tuple[MemorySnapshot, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                _SNAPSHOT_SELECT
                + """
                WHERE r.tenant_id = %s AND r.subject_id = %s
                ORDER BY r.key, r.context_fingerprint, r.id
                """,
                (scope.tenant_id, scope.subject_id),
            ).fetchall()
        return tuple(_snapshot(row) for row in rows)

    def memories_as_of(
        self,
        scope: Scope,
        *,
        valid_at: datetime,
        known_at: datetime,
    ) -> tuple[MemorySnapshot, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                _AS_OF_SNAPSHOT_SELECT,
                (
                    known_at,
                    valid_at,
                    scope.tenant_id,
                    scope.subject_id,
                    known_at,
                ),
            ).fetchall()
        return tuple(_snapshot(row) for row in rows)

    def revision_history(
        self,
        scope: Scope,
        record_id: UUID,
    ) -> tuple[MemoryRevision, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT v.* FROM memory_revisions v
                WHERE v.tenant_id = %s AND v.subject_id = %s AND v.record_id = %s
                ORDER BY v.sequence
                """,
                (scope.tenant_id, scope.subject_id, record_id),
            ).fetchall()
        return tuple(_revision(row) for row in rows)

    def _insert_revision(
        self,
        connection: DbConnection,
        scope: Scope,
        revision: MemoryRevision,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_revisions (
                id, record_id, tenant_id, subject_id, sequence, value, confidence,
                support_count, contradiction_count, source_diversity, source_keys,
                last_evidence_at, evidence_ids, valid_from, recorded_at,
                supersedes_revision_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                revision.id,
                revision.record_id,
                scope.tenant_id,
                scope.subject_id,
                revision.sequence,
                revision.value,
                revision.belief.confidence,
                revision.belief.support_count,
                revision.belief.contradiction_count,
                revision.belief.source_diversity,
                list(revision.belief.source_keys),
                revision.belief.last_evidence_at,
                list(revision.evidence_ids),
                revision.valid_from,
                revision.recorded_at,
                revision.supersedes_revision_id,
            ),
        )

    def _insert_transition(
        self,
        connection: DbConnection,
        scope: Scope,
        transition: RevisionTransition,
    ) -> None:
        connection.execute(
            """
            INSERT INTO revision_transitions (
                id, record_id, tenant_id, subject_id, kind, occurred_at,
                to_revision_id, from_revision_id, reason
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                transition.id,
                transition.record_id,
                scope.tenant_id,
                scope.subject_id,
                transition.kind.value,
                transition.occurred_at,
                transition.to_revision_id,
                transition.from_revision_id,
                transition.reason,
            ),
        )

    def save_trace(self, trace: RecallTrace) -> None:
        try:
            with self._connection() as connection:
                connection.execute(
                    """
                    INSERT INTO recall_traces (
                        id, tenant_id, subject_id, query, context, context_fingerprint,
                        policy_id, policy_version, valid_at, known_at, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        trace.id,
                        trace.scope.tenant_id,
                        trace.scope.subject_id,
                        trace.query,
                        Jsonb(trace.context.as_dict()),
                        trace.context.fingerprint,
                        trace.policy_id,
                        trace.policy_version,
                        trace.valid_at,
                        trace.known_at,
                        trace.created_at,
                    ),
                )
                for item in trace.items:
                    connection.execute(
                        """
                        INSERT INTO recall_trace_items (
                            trace_id, revision_id, record_id, tenant_id, subject_id,
                            key, value, context, revision_valid_from, revision_recorded_at,
                            rank, score, score_breakdown, evidence_ids
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
                        )
                        """,
                        (
                            trace.id,
                            item.revision_id,
                            item.record_id,
                            trace.scope.tenant_id,
                            trace.scope.subject_id,
                            item.key,
                            item.value,
                            Jsonb(item.context.as_dict()),
                            item.revision_valid_from,
                            item.revision_recorded_at,
                            item.rank,
                            item.score,
                            Jsonb(
                                {
                                    "semantic": item.breakdown.semantic,
                                    "context": item.breakdown.context,
                                    "belief": item.breakdown.belief,
                                    "utility": item.breakdown.utility,
                                    "recency": item.breakdown.recency,
                                    "lexical": item.breakdown.lexical,
                                    "vector": item.breakdown.vector,
                                }
                            ),
                            list(item.evidence_ids),
                        ),
                    )
        except (UniqueViolation, ForeignKeyViolation, CheckViolation) as exc:
            raise ConflictError("recall trace conflicts with authoritative state") from exc

    def trace(self, scope: Scope, trace_id: UUID) -> RecallTrace | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM recall_traces
                WHERE id = %s AND tenant_id = %s AND subject_id = %s
                """,
                (trace_id, scope.tenant_id, scope.subject_id),
            ).fetchone()
            if row is None:
                return None
            item_rows = connection.execute(
                """
                SELECT * FROM recall_trace_items
                WHERE trace_id = %s AND tenant_id = %s AND subject_id = %s
                ORDER BY rank
                """,
                (trace_id, scope.tenant_id, scope.subject_id),
            ).fetchall()
        return RecallTrace(
            id=row["id"],
            scope=scope,
            query=row["query"],
            context=_context(row["context"]),
            policy_id=row["policy_id"],
            policy_version=row["policy_version"],
            items=tuple(_recalled_item(item) for item in item_rows),
            valid_at=row["valid_at"],
            known_at=row["known_at"],
            created_at=row["created_at"],
        )

    def utility_for(
        self,
        scope: Scope,
        revision_id: UUID,
        context: ContextSignature,
    ) -> UtilityEstimate:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM utility_estimates
                WHERE tenant_id = %s AND subject_id = %s
                  AND revision_id = %s AND context_fingerprint = %s
                """,
                (
                    scope.tenant_id,
                    scope.subject_id,
                    revision_id,
                    context.fingerprint,
                ),
            ).fetchone()
        if row is None:
            return UtilityEstimate(
                revision_id=revision_id,
                context_fingerprint=context.fingerprint,
            )
        return _utility(row)

    def utility_for_as_of(
        self,
        scope: Scope,
        revision_id: UUID,
        context: ContextSignature,
        *,
        known_at: datetime,
    ) -> UtilityEstimate:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT
                    COALESCE(
                        SUM(o.weight) FILTER (WHERE o.kind IN ('helpful', 'accepted')),
                        0.0
                    ) AS positive_weight,
                    COALESCE(
                        SUM(o.weight) FILTER (
                            WHERE o.kind IN ('harmful', 'rejected', 'corrected')
                        ),
                        0.0
                    ) AS negative_weight,
                    MAX(o.occurred_at) AS last_outcome_at
                FROM outcomes o
                JOIN recall_traces t
                  ON t.id = o.trace_id
                 AND t.tenant_id = o.tenant_id
                 AND t.subject_id = o.subject_id
                WHERE o.tenant_id = %s AND o.subject_id = %s
                  AND o.revision_id = %s
                  AND t.context_fingerprint = %s
                  AND o.recorded_at <= %s
                """,
                (
                    scope.tenant_id,
                    scope.subject_id,
                    revision_id,
                    context.fingerprint,
                    known_at,
                ),
            ).fetchone()
        if row is None:
            raise ConflictError("historical utility aggregation returned no row")
        return UtilityEstimate(
            revision_id=revision_id,
            context_fingerprint=context.fingerprint,
            positive_weight=float(row["positive_weight"]),
            negative_weight=float(row["negative_weight"]),
            last_outcome_at=row["last_outcome_at"],
        )

    def apply_outcome(
        self,
        outcome: OutcomeEvent,
        context: ContextSignature,
    ) -> tuple[OutcomeEvent, UtilityEstimate, bool]:
        with self._connection() as connection:
            existing_row = self._outcome_by_idempotency(connection, outcome)
            if existing_row is not None:
                return self._replayed_outcome(existing_row, outcome, context)

            try:
                utility_row = self._insert_outcome(connection, outcome, context)
            except UniqueViolation:
                existing_row = self._outcome_by_idempotency(connection, outcome)
                if existing_row is None:
                    raise ConflictError("outcome conflicts with authoritative state") from None
                return self._replayed_outcome(existing_row, outcome, context)
            except (ForeignKeyViolation, CheckViolation) as exc:
                raise ConflictError("outcome conflicts with trace attribution") from exc
        return outcome, _utility(utility_row), True

    def _insert_outcome(
        self,
        connection: DbConnection,
        outcome: OutcomeEvent,
        context: ContextSignature,
    ) -> DbRow:
        with connection.transaction():
            connection.execute(
                """
                INSERT INTO outcomes (
                    id, tenant_id, subject_id, trace_id, revision_id, kind,
                    idempotency_key, occurred_at, recorded_at, weight, note
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    outcome.id,
                    outcome.scope.tenant_id,
                    outcome.scope.subject_id,
                    outcome.trace_id,
                    outcome.revision_id,
                    outcome.kind.value,
                    outcome.idempotency_key,
                    outcome.occurred_at,
                    outcome.recorded_at,
                    outcome.weight,
                    outcome.note,
                ),
            )
            success = outcome.kind.success_value
            utility_row = connection.execute(
                """
                INSERT INTO utility_estimates (
                    tenant_id, subject_id, revision_id, context_fingerprint,
                    positive_weight, negative_weight, last_outcome_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, subject_id, revision_id, context_fingerprint)
                DO UPDATE SET
                    positive_weight =
                        utility_estimates.positive_weight + EXCLUDED.positive_weight,
                    negative_weight =
                        utility_estimates.negative_weight + EXCLUDED.negative_weight,
                    last_outcome_at = GREATEST(
                        utility_estimates.last_outcome_at,
                        EXCLUDED.last_outcome_at
                    )
                RETURNING *
                """,
                (
                    outcome.scope.tenant_id,
                    outcome.scope.subject_id,
                    outcome.revision_id,
                    context.fingerprint,
                    success * outcome.weight,
                    (1.0 - success) * outcome.weight,
                    outcome.occurred_at,
                ),
            ).fetchone()
            if utility_row is None:
                raise ConflictError("utility update did not return authoritative state")
            self._append_outbox(
                connection,
                aggregate_type="outcome",
                aggregate_id=outcome.id,
                event_type="outcome.recorded",
                occurred_at=outcome.recorded_at,
                payload={
                    "tenant_id": outcome.scope.tenant_id,
                    "subject_id": outcome.scope.subject_id,
                    "trace_id": str(outcome.trace_id),
                    "revision_id": str(outcome.revision_id),
                    "kind": outcome.kind.value,
                    "occurred_at": outcome.occurred_at.isoformat(),
                },
            )
            return utility_row

    def _outcome_by_idempotency(
        self,
        connection: DbConnection,
        outcome: OutcomeEvent,
    ) -> DbRow | None:
        return connection.execute(
            """
            SELECT * FROM outcomes
            WHERE tenant_id = %s AND subject_id = %s AND idempotency_key = %s
            """,
            (
                outcome.scope.tenant_id,
                outcome.scope.subject_id,
                outcome.idempotency_key,
            ),
        ).fetchone()

    def _replayed_outcome(
        self,
        row: DbRow,
        requested: OutcomeEvent,
        context: ContextSignature,
    ) -> tuple[OutcomeEvent, UtilityEstimate, bool]:
        existing = _outcome(row)
        if (
            existing.trace_id != requested.trace_id
            or existing.revision_id != requested.revision_id
            or existing.kind != requested.kind
            or existing.weight != requested.weight
        ):
            raise ConflictError("outcome idempotency key was reused with different data")
        utility = self.utility_for(requested.scope, requested.revision_id, context)
        return existing, utility, False

    def save_strategy(self, strategy: StrategySnapshot) -> None:
        """Persist an immutable strategy before traces can reference it."""
        with self._connection() as connection:
            self._save_strategy(connection, strategy)

    def strategy(self, strategy_id: UUID) -> StrategySnapshot | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategy_snapshots WHERE id = %s",
                (strategy_id,),
            ).fetchone()
        return _strategy(row) if row is not None else None

    def ensure_active_strategy(
        self,
        strategy: StrategySnapshot,
        activation: StrategyActivation,
    ) -> StrategySnapshot:
        with self._connection() as connection, connection.transaction():
            connection.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (_STRATEGY_ACTIVATION_LOCK_ID,),
            )
            active = self._active_strategy_row(connection)
            if active is not None:
                return _strategy(active)
            if (
                activation.kind is not StrategyActivationKind.BOOTSTRAP
                or activation.strategy_id != strategy.id
                or strategy.parent_id is not None
                or strategy.version != 1
            ):
                raise ConflictError("initial active strategy requires a root bootstrap activation")
            self._save_strategy(connection, strategy)
            connection.execute(
                """
                INSERT INTO strategy_activations (
                    id, strategy_id, previous_strategy_id, kind,
                    activated_at, reason, experiment_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    activation.id,
                    activation.strategy_id,
                    activation.previous_strategy_id,
                    activation.kind.value,
                    activation.activated_at,
                    activation.reason,
                    activation.experiment_id,
                ),
            )
            return strategy

    def active_strategy(self) -> StrategySnapshot | None:
        with self._connection() as connection:
            row = self._active_strategy_row(connection)
        return _strategy(row) if row is not None else None

    def strategy_activation_history(self) -> tuple[StrategyActivation, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM strategy_activations ORDER BY sequence"
            ).fetchall()
        return tuple(_strategy_activation(row) for row in rows)

    def register_evolution_experiment(
        self,
        candidate: StrategySnapshot,
        experiment: EvolutionExperiment,
        transition: ExperimentTransition,
    ) -> None:
        with (
            self._translate_evolution_conflicts(),
            self._connection() as connection,
            connection.transaction(),
        ):
            self._lock_strategy_activations(connection)
            active_row = self._active_strategy_row(connection)
            if active_row is None:
                raise ConflictError("experiment baseline is not the active strategy")
            active = _strategy(active_row)
            if active.id != experiment.baseline_id:
                raise ConflictError("experiment baseline is not the active strategy")
            if (
                candidate.id != experiment.candidate_id
                or candidate.parent_id != active.id
                or candidate.version != active.version + 1
            ):
                raise ConflictError("experiment candidate does not extend the active baseline")
            if (
                experiment.stage is not ExperimentStage.PROPOSED
                or experiment.created_at != experiment.updated_at
                or transition.experiment_id != experiment.id
                or transition.from_stage is not None
                or transition.to_stage is not ExperimentStage.PROPOSED
                or transition.transitioned_at != experiment.created_at
            ):
                raise ConflictError("experiment creation evidence is inconsistent")
            if (
                connection.execute(
                    "SELECT 1 FROM evolution_experiments WHERE id = %s",
                    (experiment.id,),
                ).fetchone()
                is not None
            ):
                raise ConflictError("evolution experiment already exists")
            self._save_strategy(connection, candidate)
            connection.execute(
                """
                INSERT INTO evolution_experiments (
                    id, baseline_id, candidate_id, stage, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (
                    experiment.id,
                    experiment.baseline_id,
                    experiment.candidate_id,
                    experiment.stage.value,
                    experiment.created_at,
                    experiment.updated_at,
                ),
            )
            self._insert_experiment_transition(connection, transition)

    def evolution_experiment(self, experiment_id: UUID) -> EvolutionExperiment | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM evolution_experiments WHERE id = %s",
                (experiment_id,),
            ).fetchone()
        return _evolution_experiment(row) if row is not None else None

    def experiment_transition_history(
        self,
        experiment_id: UUID,
    ) -> tuple[ExperimentTransition, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM evolution_experiment_transitions
                WHERE experiment_id = %s
                ORDER BY sequence
                """,
                (experiment_id,),
            ).fetchall()
        return tuple(_experiment_transition(row) for row in rows)

    def experiment_transition_by_idempotency(
        self,
        idempotency_key: str,
    ) -> ExperimentTransition | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM evolution_experiment_transitions
                WHERE idempotency_key = %s
                """,
                (idempotency_key.strip(),),
            ).fetchone()
        return _experiment_transition(row) if row is not None else None

    def advance_evolution_experiment(
        self,
        experiment: EvolutionExperiment,
        transition: ExperimentTransition,
        activation: StrategyActivation | None = None,
    ) -> None:
        with (
            self._translate_evolution_conflicts(),
            self._connection() as connection,
            connection.transaction(),
        ):
            self._lock_strategy_activations(connection)
            row = connection.execute(
                "SELECT * FROM evolution_experiments WHERE id = %s FOR UPDATE",
                (experiment.id,),
            ).fetchone()
            if row is None:
                raise ConflictError("evolution experiment does not exist")
            current = _evolution_experiment(row)
            if (
                experiment.baseline_id != current.baseline_id
                or experiment.candidate_id != current.candidate_id
                or experiment.created_at != current.created_at
                or transition.experiment_id != current.id
                or transition.from_stage is not current.stage
                or transition.to_stage is not experiment.stage
                or transition.transitioned_at != experiment.updated_at
            ):
                raise ConflictError("experiment transition evidence is inconsistent")
            expected = current.transition(experiment.stage, transition.transitioned_at)
            if experiment != expected:
                raise ConflictError("experiment transition does not match current state")
            self._validate_experiment_activation(
                connection, current, experiment, transition, activation
            )
            updated = connection.execute(
                """
                UPDATE evolution_experiments
                SET stage = %s, updated_at = %s
                WHERE id = %s AND stage = %s
                RETURNING id
                """,
                (
                    experiment.stage.value,
                    experiment.updated_at,
                    experiment.id,
                    current.stage.value,
                ),
            ).fetchone()
            if updated is None:
                raise ConflictError("evolution experiment changed concurrently")
            self._insert_experiment_transition(connection, transition)
            if activation is not None:
                self._insert_strategy_activation(connection, activation)

    def _validate_experiment_activation(
        self,
        connection: DbConnection,
        current: EvolutionExperiment,
        updated: EvolutionExperiment,
        transition: ExperimentTransition,
        activation: StrategyActivation | None,
    ) -> None:
        promotion = (
            current.stage is ExperimentStage.CANARY and updated.stage is ExperimentStage.PROMOTED
        )
        rollback = (
            current.stage is ExperimentStage.PROMOTED
            and updated.stage is ExperimentStage.ROLLED_BACK
        )
        if not promotion and not rollback:
            if activation is not None:
                raise ConflictError("this experiment transition cannot activate a strategy")
            return
        if activation is None:
            raise ConflictError("strategy activation evidence is missing or duplicated")
        expected_kind = (
            StrategyActivationKind.PROMOTION if promotion else StrategyActivationKind.ROLLBACK
        )
        expected_target = current.candidate_id if promotion else current.baseline_id
        expected_previous = current.baseline_id if promotion else current.candidate_id
        active_row = self._active_strategy_row(connection)
        active = _strategy(active_row) if active_row is not None else None
        target_exists = connection.execute(
            "SELECT 1 FROM strategy_snapshots WHERE id = %s",
            (expected_target,),
        ).fetchone()
        if (
            activation.kind is not expected_kind
            or activation.strategy_id != expected_target
            or activation.previous_strategy_id != expected_previous
            or activation.experiment_id != current.id
            or activation.activated_at != transition.transitioned_at
            or activation.reason != transition.reason
            or active is None
            or active.id != expected_previous
            or target_exists is None
        ):
            raise ConflictError("strategy activation does not match experiment state")

    def _insert_experiment_transition(
        self,
        connection: DbConnection,
        transition: ExperimentTransition,
    ) -> None:
        connection.execute(
            """
            INSERT INTO evolution_experiment_transitions (
                id, experiment_id, from_stage, to_stage,
                transitioned_at, reason, evidence_ref,
                idempotency_key, request_fingerprint
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                transition.id,
                transition.experiment_id,
                transition.from_stage.value if transition.from_stage is not None else None,
                transition.to_stage.value,
                transition.transitioned_at,
                transition.reason,
                transition.evidence_ref,
                transition.idempotency_key,
                transition.request_fingerprint,
            ),
        )

    def _insert_strategy_activation(
        self,
        connection: DbConnection,
        activation: StrategyActivation,
    ) -> None:
        connection.execute(
            """
            INSERT INTO strategy_activations (
                id, strategy_id, previous_strategy_id, kind,
                activated_at, reason, experiment_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                activation.id,
                activation.strategy_id,
                activation.previous_strategy_id,
                activation.kind.value,
                activation.activated_at,
                activation.reason,
                activation.experiment_id,
            ),
        )

    def _lock_strategy_activations(self, connection: DbConnection) -> None:
        connection.execute(
            "SELECT pg_advisory_xact_lock(%s)",
            (_STRATEGY_ACTIVATION_LOCK_ID,),
        )

    @contextmanager
    def _translate_evolution_conflicts(self) -> Iterator[None]:
        try:
            yield
        except (CheckViolation, ForeignKeyViolation, UniqueViolation) as error:
            raise ConflictError("evolution persistence conflict") from error

    def _active_strategy_row(self, connection: DbConnection) -> DbRow | None:
        return connection.execute(
            """
            SELECT strategy.*
            FROM strategy_activations AS activation
            JOIN strategy_snapshots AS strategy ON strategy.id = activation.strategy_id
            ORDER BY activation.sequence DESC
            LIMIT 1
            """
        ).fetchone()

    def _save_strategy(
        self,
        connection: DbConnection,
        strategy: StrategySnapshot,
    ) -> None:
        weights = {
            "semantic": strategy.weights.semantic,
            "context": strategy.weights.context,
            "belief": strategy.weights.belief,
            "utility": strategy.weights.utility,
            "recency": strategy.weights.recency,
        }
        existing = connection.execute(
            "SELECT * FROM strategy_snapshots WHERE id = %s",
            (strategy.id,),
        ).fetchone()
        if existing is not None:
            if _strategy(existing) != strategy:
                raise ConflictError("strategy id already belongs to a different snapshot")
            return
        if strategy.parent_id is None:
            if strategy.version != 1:
                raise ConflictError("root strategy version must be 1")
        else:
            parent = connection.execute(
                "SELECT version FROM strategy_snapshots WHERE id = %s",
                (strategy.parent_id,),
            ).fetchone()
            if parent is None or strategy.version != parent["version"] + 1:
                raise ConflictError("strategy parent and version are inconsistent")
        inserted = connection.execute(
            """
            INSERT INTO strategy_snapshots (
                id, version, weights, min_score, recency_half_life_days,
                created_at, parent_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            (
                strategy.id,
                strategy.version,
                Jsonb(weights),
                strategy.min_score,
                strategy.recency_half_life_days,
                strategy.created_at,
                strategy.parent_id,
            ),
        ).fetchone()
        if inserted is None:
            concurrent = connection.execute(
                "SELECT * FROM strategy_snapshots WHERE id = %s",
                (strategy.id,),
            ).fetchone()
            if concurrent is None or _strategy(concurrent) != strategy:
                raise ConflictError("strategy id already belongs to a different snapshot")

    def _revision_outbox(
        self,
        connection: DbConnection,
        scope: Scope,
        revision: MemoryRevision,
        event_type: str,
    ) -> None:
        self._append_outbox(
            connection,
            aggregate_type="memory_revision",
            aggregate_id=revision.id,
            event_type=event_type,
            occurred_at=revision.recorded_at,
            payload={
                "tenant_id": scope.tenant_id,
                "subject_id": scope.subject_id,
                "record_id": str(revision.record_id),
                "revision_id": str(revision.id),
                "sequence": revision.sequence,
            },
        )

    def _append_outbox(
        self,
        connection: DbConnection,
        *,
        aggregate_type: str,
        aggregate_id: UUID,
        event_type: str,
        occurred_at: datetime,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO outbox_events (
                id, aggregate_type, aggregate_id, event_type, payload, occurred_at
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (uuid4(), aggregate_type, aggregate_id, event_type, Jsonb(dict(payload)), occurred_at),
        )


_SNAPSHOT_SELECT = """
    SELECT
        r.id AS record_id,
        r.tenant_id,
        r.subject_id,
        r.kind AS record_kind,
        r.key AS record_key,
        r.context AS record_context,
        r.created_at AS record_created_at,
        v.id AS revision_id,
        v.sequence,
        v.value,
        v.confidence,
        v.support_count,
        v.contradiction_count,
        v.source_diversity,
        v.source_keys,
        v.last_evidence_at,
        v.evidence_ids,
        v.valid_from,
        v.recorded_at,
        v.supersedes_revision_id
    FROM memory_records r
    JOIN memory_revisions v ON v.id = r.active_revision_id
"""


_AS_OF_SNAPSHOT_SELECT = """
    SELECT
        r.id AS record_id,
        r.tenant_id,
        r.subject_id,
        r.kind AS record_kind,
        r.key AS record_key,
        r.context AS record_context,
        r.created_at AS record_created_at,
        v.id AS revision_id,
        v.sequence,
        v.value,
        v.confidence,
        v.support_count,
        v.contradiction_count,
        v.source_diversity,
        v.source_keys,
        v.last_evidence_at,
        v.evidence_ids,
        v.valid_from,
        v.recorded_at,
        v.supersedes_revision_id
    FROM memory_records r
    JOIN LATERAL (
        SELECT candidate.*
        FROM memory_revisions candidate
        WHERE candidate.record_id = r.id
          AND candidate.tenant_id = r.tenant_id
          AND candidate.subject_id = r.subject_id
          AND candidate.recorded_at <= %s
          AND candidate.valid_from <= %s
        ORDER BY candidate.recorded_at DESC, candidate.sequence DESC, candidate.id DESC
        LIMIT 1
    ) v ON TRUE
    WHERE r.tenant_id = %s AND r.subject_id = %s AND r.created_at <= %s
    ORDER BY r.key, r.context_fingerprint, r.id
"""


def _strategy(row: DbRow) -> StrategySnapshot:
    raw_weights = row["weights"]
    if not isinstance(raw_weights, dict):
        raise ConflictError("stored strategy weights are not an object")
    try:
        weights = RetrievalWeights(
            semantic=float(raw_weights["semantic"]),
            context=float(raw_weights["context"]),
            belief=float(raw_weights["belief"]),
            utility=float(raw_weights["utility"]),
            recency=float(raw_weights["recency"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ConflictError("stored strategy weights are invalid") from error
    return StrategySnapshot(
        id=row["id"],
        version=row["version"],
        weights=weights,
        min_score=row["min_score"],
        recency_half_life_days=row["recency_half_life_days"],
        created_at=row["created_at"],
        parent_id=row["parent_id"],
    )


def _strategy_activation(row: DbRow) -> StrategyActivation:
    return StrategyActivation(
        id=row["id"],
        strategy_id=row["strategy_id"],
        previous_strategy_id=row["previous_strategy_id"],
        kind=StrategyActivationKind(row["kind"]),
        activated_at=row["activated_at"],
        reason=row["reason"],
        experiment_id=row["experiment_id"],
    )


def _evolution_experiment(row: DbRow) -> EvolutionExperiment:
    return EvolutionExperiment(
        id=row["id"],
        baseline_id=row["baseline_id"],
        candidate_id=row["candidate_id"],
        stage=ExperimentStage(row["stage"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _experiment_transition(row: DbRow) -> ExperimentTransition:
    raw_from_stage = row["from_stage"]
    return ExperimentTransition(
        id=row["id"],
        experiment_id=row["experiment_id"],
        from_stage=(ExperimentStage(raw_from_stage) if raw_from_stage is not None else None),
        to_stage=ExperimentStage(row["to_stage"]),
        transitioned_at=row["transitioned_at"],
        reason=row["reason"],
        evidence_ref=row["evidence_ref"],
        idempotency_key=row["idempotency_key"],
        request_fingerprint=row["request_fingerprint"],
    )


def _observation(row: DbRow) -> Observation:
    metadata_value = row["metadata"]
    metadata = tuple((str(item[0]), str(item[1])) for item in metadata_value)
    return Observation(
        id=row["id"],
        scope=Scope(row["tenant_id"], row["subject_id"]),
        kind=ObservationKind(row["kind"]),
        source=row["source"],
        content=row["content"],
        idempotency_key=row["idempotency_key"],
        occurred_at=row["occurred_at"],
        ingested_at=row["ingested_at"],
        metadata=metadata,
    )


def _candidate(row: DbRow) -> Candidate:
    return Candidate(
        id=row["id"],
        scope=Scope(row["tenant_id"], row["subject_id"]),
        observation_id=row["observation_id"],
        key=row["key"],
        value=row["value"],
        context=_context(row["context"]),
        evidence_ids=_uuid_tuple(row["evidence_ids"]),
        confidence=row["confidence"],
        proposed_at=row["proposed_at"],
        state=CandidateState(row["state"]),
        accepted_record_id=row["accepted_record_id"],
        accepted_revision_id=row["accepted_revision_id"],
    )


def _snapshot(row: DbRow) -> MemorySnapshot:
    scope = Scope(row["tenant_id"], row["subject_id"])
    record = MemoryRecord(
        id=row["record_id"],
        scope=scope,
        kind=MemoryKind(row["record_kind"]),
        key=row["record_key"],
        context=_context(row["record_context"]),
        created_at=row["record_created_at"],
    )
    revision = MemoryRevision(
        id=row["revision_id"],
        record_id=row["record_id"],
        sequence=row["sequence"],
        value=row["value"],
        belief=_belief(row),
        evidence_ids=_uuid_tuple(row["evidence_ids"]),
        valid_from=row["valid_from"],
        recorded_at=row["recorded_at"],
        supersedes_revision_id=row["supersedes_revision_id"],
    )
    return MemorySnapshot(record=record, revision=revision)


def _revision(row: DbRow) -> MemoryRevision:
    return MemoryRevision(
        id=row["id"],
        record_id=row["record_id"],
        sequence=row["sequence"],
        value=row["value"],
        belief=_belief(row),
        evidence_ids=_uuid_tuple(row["evidence_ids"]),
        valid_from=row["valid_from"],
        recorded_at=row["recorded_at"],
        supersedes_revision_id=row["supersedes_revision_id"],
    )


def _belief(row: DbRow) -> BeliefState:
    return BeliefState(
        confidence=row["confidence"],
        support_count=row["support_count"],
        contradiction_count=row["contradiction_count"],
        source_diversity=row["source_diversity"],
        source_keys=tuple(str(source) for source in row["source_keys"]),
        last_evidence_at=row["last_evidence_at"],
    )


def _recalled_item(row: DbRow) -> RecalledItem:
    raw_breakdown = row["score_breakdown"]
    return RecalledItem(
        record_id=row["record_id"],
        revision_id=row["revision_id"],
        key=row["key"],
        value=row["value"],
        context=_context(row["context"]),
        revision_valid_from=row["revision_valid_from"],
        revision_recorded_at=row["revision_recorded_at"],
        rank=row["rank"],
        score=row["score"],
        breakdown=ScoreBreakdown(
            semantic=float(raw_breakdown["semantic"]),
            context=float(raw_breakdown["context"]),
            belief=float(raw_breakdown["belief"]),
            utility=float(raw_breakdown["utility"]),
            recency=float(raw_breakdown["recency"]),
            lexical=(
                float(raw_breakdown["lexical"])
                if raw_breakdown.get("lexical") is not None
                else None
            ),
            vector=(
                float(raw_breakdown["vector"]) if raw_breakdown.get("vector") is not None else None
            ),
        ),
        evidence_ids=_uuid_tuple(row["evidence_ids"]),
    )


def _outcome(row: DbRow) -> OutcomeEvent:
    return OutcomeEvent(
        id=row["id"],
        scope=Scope(row["tenant_id"], row["subject_id"]),
        trace_id=row["trace_id"],
        revision_id=row["revision_id"],
        kind=OutcomeKind(row["kind"]),
        idempotency_key=row["idempotency_key"],
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
        weight=row["weight"],
        note=row["note"],
    )


def _utility(row: DbRow) -> UtilityEstimate:
    return UtilityEstimate(
        revision_id=row["revision_id"],
        context_fingerprint=row["context_fingerprint"],
        positive_weight=row["positive_weight"],
        negative_weight=row["negative_weight"],
        last_outcome_at=row["last_outcome_at"],
    )


def _context(value: object) -> ContextSignature:
    if not isinstance(value, dict):
        raise ConflictError("stored context is not an object")
    return ContextSignature.from_mapping({str(key): str(item) for key, item in value.items()})


def _uuid_tuple(value: object) -> tuple[UUID, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConflictError("stored UUID collection is invalid")
    return tuple(item if isinstance(item, UUID) else UUID(str(item)) for item in value)
