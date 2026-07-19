from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
import pytest
from alembic import command as alembic_command
from fastapi.testclient import TestClient
from psycopg.errors import CheckViolation, ForeignKeyViolation

from conftest import FixedClock, SequentialIds, prepare_postgres_database
from evolvable_memory.adapters.authorization import PostgresAuthorizationAuditSink
from evolvable_memory.adapters.gate_receipts import (
    HmacGateReceiptSigner,
    HmacGateReceiptVerifier,
)
from evolvable_memory.adapters.postgres import PostgresMemoryStore
from evolvable_memory.adapters.postgres_governance import PostgresPrivacyGovernance
from evolvable_memory.adapters.system import Uuid4Generator
from evolvable_memory.api.app import create_app
from evolvable_memory.application.commands import (
    CorrectPreference,
    ProjectRecallContext,
    RecallMemory,
    RecordMemoryUsage,
    RecordOutcome,
    RememberPreference,
)
from evolvable_memory.application.evolution import (
    EvolutionApplication,
    EvolutionProposal,
    EvolutionTransitionResult,
)
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.config import Settings
from evolvable_memory.domain.common import ConflictError, ContextSignature, NotFoundError, Scope
from evolvable_memory.domain.evolution import (
    EvolutionExperiment,
    ExperimentStage,
    FailureDiagnosis,
    GateDecision,
    GateReceipt,
    StrategyActivationKind,
    StrategySnapshot,
)
from evolvable_memory.domain.experience import OutcomeKind
from evolvable_memory.migrate import alembic_config
from fault_proxy import TcpFaultProxy, database_url_through_proxy, postgres_target

pytestmark = pytest.mark.postgres

_GATE_ISSUER = "postgres-test-evaluator"
_GATE_KEY_ID = "postgres-test-key-v1"
_GATE_SECRET = b"postgres-test-gate-receipt-secret-32-bytes-minimum"
_GATE_SIGNER = HmacGateReceiptSigner(
    issuer=_GATE_ISSUER,
    key_id=_GATE_KEY_ID,
    secret=_GATE_SECRET,
)


def _evolution_application(
    store: PostgresMemoryStore,
    clock: FixedClock,
    ids: SequentialIds | Uuid4Generator,
) -> EvolutionApplication:
    return EvolutionApplication(
        store=store,
        clock=clock,
        ids=ids,
        gate_verifier=HmacGateReceiptVerifier({(_GATE_ISSUER, _GATE_KEY_ID): _GATE_SECRET}),
    )


def _gate_receipt(
    experiment: EvolutionExperiment,
    clock: FixedClock,
    target: ExperimentStage,
    *,
    reason: str,
    evidence_ref: str,
) -> GateReceipt:
    if target is ExperimentStage.REJECTED:
        decision = GateDecision.REJECT
    elif target is ExperimentStage.ROLLED_BACK:
        decision = GateDecision.ROLLBACK
    else:
        decision = GateDecision.PASS
    identity = "|".join(
        (
            str(experiment.id),
            experiment.stage.value,
            target.value,
            reason,
            evidence_ref,
            clock.now().isoformat(),
        )
    )
    return _GATE_SIGNER.issue(
        receipt_id=uuid5(NAMESPACE_URL, identity),
        experiment=experiment,
        target=target,
        decision=decision,
        artifact_ref=evidence_ref,
        artifact_sha256=sha256(evidence_ref.encode()).hexdigest(),
        issued_at=clock.now(),
        expires_at=clock.now() + timedelta(minutes=5),
        hard_gates_passed=decision is GateDecision.PASS,
        reason=reason,
    )


def _advance_evolution(
    application: EvolutionApplication,
    store: PostgresMemoryStore,
    clock: FixedClock,
    experiment_id: UUID,
    target: ExperimentStage,
    *,
    reason: str,
    evidence_ref: str,
    idempotency_key: str,
) -> EvolutionTransitionResult:
    current = store.evolution_experiment(experiment_id)
    assert current is not None
    existing = store.experiment_transition_by_idempotency(idempotency_key.strip())
    receipt_experiment = current
    if (
        existing is not None
        and existing.experiment_id == experiment_id
        and existing.to_stage is target
        and existing.from_stage is not None
    ):
        receipt_experiment = replace(current, stage=existing.from_stage)
    return application.advance(
        experiment_id,
        target,
        receipt=_gate_receipt(
            receipt_experiment,
            clock,
            target,
            reason=reason,
            evidence_ref=evidence_ref,
        ),
        idempotency_key=idempotency_key,
    )


def test_postgres_store_rejects_nonpositive_readiness_timeout() -> None:
    with pytest.raises(ValueError, match="readiness_timeout"):
        PostgresMemoryStore(
            "postgresql://unused:unused@127.0.0.1:1/unused",
            readiness_timeout=0,
        )


@pytest.fixture
def postgres_url() -> Iterator[str]:
    database_url = os.getenv("EMF_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set EMF_TEST_DATABASE_URL to run PostgreSQL integration tests")

    prepare_postgres_database(database_url)
    yield database_url


def _preference(scope: Scope, context: ContextSignature, key: str) -> RememberPreference:
    return RememberPreference(
        scope=scope,
        source="conversation",
        idempotency_key=key,
        key="drink.preference",
        value="decaf coffee",
        context=context,
        evidence_text="晚上我只喝低因咖啡",
        confidence=0.92,
        occurred_at=datetime(2026, 7, 14, 4, 0, tzinfo=UTC),
    )


def test_postgres_store_persists_the_attributable_memory_loop(postgres_url: str) -> None:
    scope = Scope("tenant-a", "alice")
    other_scope = Scope("tenant-a", "bob")
    context = ContextSignature.from_mapping({"time_of_day": "evening"})
    clock = FixedClock()
    store = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
    app = MemoryApplication(store=store, clock=clock, ids=SequentialIds())
    original_policy = app.retrieval_policy

    assert app.is_ready()
    activation_history = store.strategy_activation_history()
    assert [item.kind for item in activation_history] == [StrategyActivationKind.BOOTSTRAP]
    conninfo = postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(conninfo, autocommit=True) as connection:
        with pytest.raises(CheckViolation, match="append-only"):
            connection.execute(
                "UPDATE strategy_activations SET reason = 'changed' WHERE id = %s",
                (activation_history[0].id,),
            )
        with pytest.raises(CheckViolation, match="append-only"):
            connection.execute(
                "DELETE FROM strategy_activations WHERE id = %s",
                (activation_history[0].id,),
            )
    with pytest.raises(ConflictError, match="strategy id"):
        store.save_strategy(replace(app.retrieval_policy, min_score=0.3))
    with pytest.raises(RuntimeError, match="force rollback"), store.transaction():
        app.remember_preference(_preference(scope, context, "rolled-back"))
        raise RuntimeError("force rollback")
    assert app.list_preferences(scope) == ()

    created = app.remember_preference(_preference(scope, context, "turn-1"))
    replay = app.remember_preference(_preference(scope, context, "turn-1"))
    assert replay == created.__class__(
        observation_id=created.observation_id,
        candidate_id=created.candidate_id,
        record_id=created.record_id,
        revision_id=created.revision_id,
        sequence=created.sequence,
        idempotent_replay=True,
    )
    assert app.list_preferences(other_scope) == ()

    trace = app.recall(
        RecallMemory(
            scope=scope,
            query="晚上喝什么饮料",
            context=context,
            limit=5,
        )
    )
    assert [item.revision_id for item in trace.items] == [created.revision_id]
    assert store.trace(other_scope, trace.id) is None
    context_projection = app.project_recall_context(
        ProjectRecallContext(scope=scope, trace_id=trace.id, budget_characters=2_000)
    )
    assert context_projection.source_revision_ids == (created.revision_id,)
    with pytest.raises(NotFoundError, match="trace not found"):
        app.project_recall_context(
            ProjectRecallContext(scope=other_scope, trace_id=trace.id, budget_characters=2_000)
        )

    usage = app.record_usage(
        RecordMemoryUsage(
            scope=scope,
            trace_id=trace.id,
            algorithm=context_projection.algorithm,
            budget_characters=context_projection.budget_characters,
            source_projection_sha256=context_projection.projection_sha256,
            delivered_context_sha256=sha256(context_projection.content.encode()).hexdigest(),
            revision_ids=(created.revision_id,),
            idempotency_key="task-1:usage",
            occurred_at=clock.now(),
        )
    )
    usage_replay = app.record_usage(
        RecordMemoryUsage(
            scope=scope,
            trace_id=trace.id,
            algorithm=context_projection.algorithm,
            budget_characters=context_projection.budget_characters,
            source_projection_sha256=context_projection.projection_sha256,
            delivered_context_sha256=sha256(context_projection.content.encode()).hexdigest(),
            revision_ids=(created.revision_id,),
            idempotency_key="task-1:usage",
            occurred_at=clock.now(),
        )
    )
    assert usage_replay.idempotent_replay
    assert usage_replay.usage.id == usage.usage.id

    outcome = app.record_outcome(
        RecordOutcome(
            scope=scope,
            trace_id=trace.id,
            revision_id=created.revision_id,
            usage_id=usage.usage.id,
            kind=OutcomeKind.HELPFUL,
            idempotency_key="task-1:outcome",
            occurred_at=clock.now(),
            note="accepted",
        )
    )
    outcome_replay = app.record_outcome(
        RecordOutcome(
            scope=scope,
            trace_id=trace.id,
            revision_id=created.revision_id,
            usage_id=usage.usage.id,
            kind=OutcomeKind.HELPFUL,
            idempotency_key="task-1:outcome",
            occurred_at=clock.now(),
            note="accepted",
        )
    )
    assert outcome.utility.mean > 0.5
    assert outcome_replay.idempotent_replay
    assert store.utility_for(other_scope, created.revision_id, context).mean == 0.5

    concurrent_app = MemoryApplication(
        store=store,
        clock=clock,
        ids=Uuid4Generator(),
        retrieval_policy=app.retrieval_policy,
    )
    concurrent_preference = _preference(
        Scope("tenant-a", "carol"),
        context,
        "turn-concurrent",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_writes = tuple(
            executor.map(
                concurrent_app.remember_preference,
                (concurrent_preference, concurrent_preference),
            )
        )
    assert sorted(result.idempotent_replay for result in concurrent_writes) == [False, True]

    concurrent_command = RecordOutcome(
        scope=scope,
        trace_id=trace.id,
        revision_id=created.revision_id,
        kind=OutcomeKind.ACCEPTED,
        idempotency_key="task-2:concurrent-outcome",
        occurred_at=clock.now(),
        note="one logical outcome",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        concurrent_results = tuple(
            executor.map(concurrent_app.record_outcome, (concurrent_command, concurrent_command))
        )
    assert sorted(result.idempotent_replay for result in concurrent_results) == [False, True]

    corrected = app.correct_preference(
        CorrectPreference(
            scope=scope,
            record_id=created.record_id,
            source="explicit-feedback",
            idempotency_key="turn-2:correction",
            value="herbal tea",
            evidence_text="其实晚上改喝花草茶",
            reason="user correction",
            occurred_at=clock.now(),
            expected_revision_id=created.revision_id,
        )
    )
    assert corrected.sequence == 2
    assert [revision.value for revision in app.history(scope, created.record_id)] == [
        "decaf coffee",
        "herbal tea",
    ]
    app.close()

    reopened_store = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
    reopened = MemoryApplication(
        store=reopened_store,
        clock=FixedClock(),
        ids=SequentialIds(),
    )
    assert reopened.retrieval_policy == original_policy
    assert len(reopened_store.strategy_activation_history()) == 1
    snapshots = reopened.list_preferences(scope)
    assert [(item.revision.value, item.revision.sequence) for item in snapshots] == [
        ("herbal tea", 2)
    ]
    persisted_trace = reopened_store.trace(scope, trace.id)
    assert persisted_trace is not None
    assert persisted_trace.valid_at == trace.valid_at
    assert persisted_trace.known_at == trace.known_at
    assert persisted_trace.items[0].revision_valid_from == trace.items[0].revision_valid_from
    assert persisted_trace.items[0].revision_recorded_at == trace.items[0].revision_recorded_at
    persisted_projection = reopened.project_recall_context(
        ProjectRecallContext(scope=scope, trace_id=trace.id, budget_characters=2_000)
    )
    assert persisted_projection == context_projection
    assert reopened_store.usage(scope, usage.usage.id) == usage.usage

    with psycopg.connect(conninfo) as connection:
        outbox_count = connection.execute("SELECT count(*) FROM outbox_events").fetchone()
        assert outbox_count is not None and outbox_count[0] >= 4
    reopened.close()
    assert not reopened.is_ready()


def test_postgres_governance_audit_and_erasure_form_a_persistent_closed_loop(
    postgres_url: str,
) -> None:
    hmac_key = b"postgres-governance-test-key-32-bytes-minimum"
    clock = FixedClock()
    scope = Scope("tenant-governed", "subject-sensitive")
    store = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
    memory = MemoryApplication(store=store, clock=clock, ids=Uuid4Generator())
    governance = PostgresPrivacyGovernance(
        postgres_url,
        hmac_key=hmac_key,
        pseudonym_key_id="test-v1",
        min_size=1,
        max_size=2,
    )
    audit = PostgresAuthorizationAuditSink(
        postgres_url,
        hmac_key=hmac_key,
        pseudonym_key_id="test-v1",
        min_size=1,
        max_size=2,
    )
    client = TestClient(
        create_app(
            memory,
            settings=Settings(store="postgres", database_url=postgres_url),
            clock=clock,
            privacy_governance=governance,
            authorization_audit=audit,
        )
    )

    grant = client.post(
        "/v1/governance/processing-grants",
        json={
            "tenant_id": scope.tenant_id,
            "subject_id": scope.subject_id,
            "purposes": ["personalization"],
            "lawful_basis": "explicit-consent",
            "idempotency_key": "postgres:grant",
            "valid_from": "2026-07-01T00:00:00Z",
        },
    )
    assert grant.status_code == 201
    preference = client.post(
        "/v1/preferences",
        json={
            "tenant_id": scope.tenant_id,
            "subject_id": scope.subject_id,
            "source": "conversation",
            "idempotency_key": "postgres:preference",
            "key": "drink.preference",
            "value": "secret decaf coffee",
            "context": {},
            "evidence_text": "secret raw evidence",
        },
    )
    assert preference.status_code == 201
    erased = client.post(
        "/v1/governance/erasures",
        json={
            "tenant_id": scope.tenant_id,
            "subject_id": scope.subject_id,
            "reason_code": "subject-request",
            "idempotency_key": "postgres:erase",
        },
    )
    assert erased.status_code == 201
    assert erased.json()["status"] == "completed"
    assert erased.json()["summary"]["observations"] == 1

    conninfo = postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(conninfo, row_factory=psycopg.rows.dict_row) as connection:
        for table in (
            "observations",
            "evidence_spans",
            "candidates",
            "memory_records",
            "memory_revisions",
            "revision_transitions",
            "recall_traces",
            "recall_trace_items",
            "memory_usage_items",
            "memory_usages",
            "outcomes",
            "utility_estimates",
            "outbox_events",
            "projection_jobs",
        ):
            row = connection.execute(f"SELECT count(*) AS count FROM {table}").fetchone()
            assert row is not None and row["count"] == 0
        audit_rows = connection.execute("SELECT * FROM authorization_audit_events").fetchall()
        erasure_row = connection.execute("SELECT * FROM erasure_requests").fetchone()
    assert len(audit_rows) == 3
    persisted = json.dumps([audit_rows, erasure_row], default=str)
    assert all(
        raw not in persisted
        for raw in (
            scope.tenant_id,
            scope.subject_id,
            "secret decaf coffee",
            "secret raw evidence",
        )
    )
    with psycopg.connect(conninfo, autocommit=True) as connection:
        with pytest.raises(CheckViolation, match="authorization audit events are append-only"):
            connection.execute("UPDATE authorization_audit_events SET reason = 'changed'")
        with pytest.raises(CheckViolation, match="suppression fences are append-only"):
            connection.execute("DELETE FROM suppression_fences")
        with pytest.raises(CheckViolation, match="completed erasure receipts are immutable"):
            connection.execute("UPDATE erasure_requests SET error_code = 'changed'")
    assert audit.is_ready()
    assert governance.is_ready()
    audit.close()
    governance.close()
    memory.close()


def test_postgres_out_of_order_outcomes_keep_utility_time_monotonic(
    postgres_url: str,
) -> None:
    scope = Scope("tenant-utility-order", "alice")
    context = ContextSignature.from_mapping({"time_of_day": "evening"})
    clock = FixedClock()
    store = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
    app = MemoryApplication(store=store, clock=clock, ids=SequentialIds())
    memory = app.remember_preference(_preference(scope, context, "utility:preference"))
    trace = app.recall(RecallMemory(scope=scope, query="decaf coffee", context=context))
    clock.advance(days=2)
    latest_outcome_at = clock.now()
    app.record_outcome(
        RecordOutcome(
            scope=scope,
            trace_id=trace.id,
            revision_id=memory.revision_id,
            kind=OutcomeKind.HELPFUL,
            idempotency_key="utility:latest-business-time",
            occurred_at=latest_outcome_at,
        )
    )
    clock.advance(seconds=1)
    late_arrival = app.record_outcome(
        RecordOutcome(
            scope=scope,
            trace_id=trace.id,
            revision_id=memory.revision_id,
            kind=OutcomeKind.HARMFUL,
            idempotency_key="utility:late-arrival",
            occurred_at=latest_outcome_at - timedelta(days=1),
        )
    )

    historical = store.utility_for_as_of(
        scope,
        memory.revision_id,
        context,
        known_at=clock.now(),
    )
    assert late_arrival.utility.last_outcome_at == latest_outcome_at
    assert historical.last_outcome_at == latest_outcome_at
    assert late_arrival.utility == historical
    app.close()


def test_postgres_concurrent_startup_bootstraps_one_active_strategy(
    postgres_url: str,
) -> None:
    store = PostgresMemoryStore(postgres_url, min_size=1, max_size=4)
    clock = FixedClock()

    def start_application() -> StrategySnapshot:
        application = MemoryApplication(store=store, clock=clock, ids=Uuid4Generator())
        return application.retrieval_policy

    with ThreadPoolExecutor(max_workers=2) as executor:
        policies = tuple(executor.map(lambda _: start_application(), range(2)))

    assert policies[0] == policies[1] == store.active_strategy()
    assert len(store.strategy_activation_history()) == 1
    conninfo = postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(conninfo) as connection:
        strategy_count = connection.execute("SELECT count(*) FROM strategy_snapshots").fetchone()
        activation_count = connection.execute(
            "SELECT count(*) FROM strategy_activations"
        ).fetchone()
    assert strategy_count == (1,)
    assert activation_count == (1,)
    store.close()


def test_postgres_persists_gated_promotion_and_atomic_rollback(
    postgres_url: str,
) -> None:
    store = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
    clock = FixedClock()
    ids = Uuid4Generator()
    memory = MemoryApplication(store=store, clock=clock, ids=ids)
    evolution = _evolution_application(store, clock, ids)
    baseline = memory.retrieval_policy
    proposal = evolution.propose(
        FailureDiagnosis(harmful_results=4),
        reason="harmful outcome diagnosis",
        evidence_ref="artifact://postgres/diagnosis",
        idempotency_key="postgres:proposal:gated",
    )
    assert proposal is not None
    proposal_replay = evolution.propose(
        FailureDiagnosis(harmful_results=4),
        reason="harmful outcome diagnosis",
        evidence_ref="artifact://postgres/diagnosis",
        idempotency_key="postgres:proposal:gated",
    )
    assert proposal_replay is not None
    assert proposal_replay.candidate == proposal.candidate
    assert proposal_replay.experiment == proposal.experiment
    assert proposal_replay.idempotent_replay is True
    assert memory.retrieval_policy == baseline

    for stage in (
        ExperimentStage.OFFLINE_PASSED,
        ExperimentStage.SHADOW,
        ExperimentStage.CANARY,
        ExperimentStage.PROMOTED,
    ):
        clock.advance(seconds=1)
        result = _advance_evolution(
            evolution,
            store,
            clock,
            proposal.experiment.id,
            stage,
            reason=f"passed {stage.value}",
            evidence_ref=f"artifact://postgres/{stage.value}",
            idempotency_key=f"postgres:advance:gated:{stage.value}",
        )
        if stage is ExperimentStage.OFFLINE_PASSED:
            replay = _advance_evolution(
                evolution,
                store,
                clock,
                proposal.experiment.id,
                stage,
                reason=f"passed {stage.value}",
                evidence_ref=f"artifact://postgres/{stage.value}",
                idempotency_key=f"postgres:advance:gated:{stage.value}",
            )
            assert replay.experiment == result.experiment
            assert replay.idempotent_replay is True

    assert memory.retrieval_policy == proposal.candidate
    assert [item.kind for item in store.strategy_activation_history()] == [
        StrategyActivationKind.BOOTSTRAP,
        StrategyActivationKind.PROMOTION,
    ]
    app_history = store.experiment_transition_history(proposal.experiment.id)
    assert [item.to_stage for item in app_history] == [
        ExperimentStage.PROPOSED,
        ExperimentStage.OFFLINE_PASSED,
        ExperimentStage.SHADOW,
        ExperimentStage.CANARY,
        ExperimentStage.PROMOTED,
    ]
    memory.close()

    reopened_store = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
    reopened_memory = MemoryApplication(
        store=reopened_store,
        clock=clock,
        ids=Uuid4Generator(),
    )
    reopened_evolution = _evolution_application(reopened_store, clock, Uuid4Generator())
    assert reopened_memory.retrieval_policy == proposal.candidate
    assert reopened_store.strategy(proposal.candidate.id) == proposal.candidate
    persisted = reopened_store.evolution_experiment(proposal.experiment.id)
    assert persisted is not None and persisted.stage is ExperimentStage.PROMOTED

    clock.advance(seconds=1)
    rolled_back = _advance_evolution(
        reopened_evolution,
        reopened_store,
        clock,
        proposal.experiment.id,
        ExperimentStage.ROLLED_BACK,
        reason="production regression rollback",
        evidence_ref="alert://postgres/rollback",
        idempotency_key="postgres:advance:gated:rollback",
    )
    assert rolled_back.experiment.stage is ExperimentStage.ROLLED_BACK
    assert reopened_memory.retrieval_policy == baseline
    assert [item.kind for item in reopened_store.strategy_activation_history()] == [
        StrategyActivationKind.BOOTSTRAP,
        StrategyActivationKind.PROMOTION,
        StrategyActivationKind.ROLLBACK,
    ]

    conninfo = postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(conninfo, autocommit=True) as connection:
        transition_id = reopened_store.experiment_transition_history(proposal.experiment.id)[0].id
        with pytest.raises(CheckViolation, match="append-only"):
            connection.execute(
                "DELETE FROM evolution_experiment_transitions WHERE id = %s",
                (transition_id,),
            )
        with pytest.raises(CheckViolation, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM evolution_experiments WHERE id = %s",
                (proposal.experiment.id,),
            )
        with pytest.raises(CheckViolation, match="illegal evolution experiment transition"):
            connection.execute(
                """
                UPDATE evolution_experiments
                SET stage = 'promoted', updated_at = updated_at + interval '1 second'
                WHERE id = %s
                """,
                (proposal.experiment.id,),
            )
    reopened_memory.close()


def test_postgres_rejects_stale_competing_promotion_without_partial_transition(
    postgres_url: str,
) -> None:
    store = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
    clock = FixedClock()
    ids = Uuid4Generator()
    memory = MemoryApplication(store=store, clock=clock, ids=ids)
    evolution = _evolution_application(store, clock, ids)
    proposals = tuple(
        evolution.propose(
            FailureDiagnosis(context_mismatches=count),
            reason=f"diagnosis {count}",
            evidence_ref=f"artifact://postgres/competing/{count}",
            idempotency_key=f"postgres:proposal:competing:{count}",
        )
        for count in (2, 3)
    )
    assert all(proposal is not None for proposal in proposals)
    first, second = proposals
    assert first is not None and second is not None
    for proposal in (first, second):
        for stage in (
            ExperimentStage.OFFLINE_PASSED,
            ExperimentStage.SHADOW,
            ExperimentStage.CANARY,
        ):
            clock.advance(seconds=1)
            _advance_evolution(
                evolution,
                store,
                clock,
                proposal.experiment.id,
                stage,
                reason=f"passed {stage.value}",
                evidence_ref=f"artifact://postgres/{proposal.experiment.id}/{stage.value}",
                idempotency_key=(f"postgres:advance:{proposal.experiment.id}:{stage.value}"),
            )

    clock.advance(seconds=1)
    _advance_evolution(
        evolution,
        store,
        clock,
        first.experiment.id,
        ExperimentStage.PROMOTED,
        reason="first promotion",
        evidence_ref="approval://postgres/first",
        idempotency_key=f"postgres:advance:{first.experiment.id}:promoted",
    )
    second_history = store.experiment_transition_history(second.experiment.id)
    clock.advance(seconds=1)
    with pytest.raises(ConflictError, match="does not match experiment state"):
        _advance_evolution(
            evolution,
            store,
            clock,
            second.experiment.id,
            ExperimentStage.PROMOTED,
            reason="stale second promotion",
            evidence_ref="approval://postgres/second",
            idempotency_key=f"postgres:advance:{second.experiment.id}:promoted",
        )

    persisted_second = store.evolution_experiment(second.experiment.id)
    assert persisted_second is not None and persisted_second.stage is ExperimentStage.CANARY
    assert store.experiment_transition_history(second.experiment.id) == second_history
    assert memory.retrieval_policy == first.candidate
    memory.close()


def test_postgres_concurrent_evolution_retries_are_idempotent(
    postgres_url: str,
) -> None:
    store = PostgresMemoryStore(postgres_url, min_size=1, max_size=4)
    clock = FixedClock()
    memory = MemoryApplication(store=store, clock=clock, ids=Uuid4Generator())
    evolution_apps = tuple(_evolution_application(store, clock, Uuid4Generator()) for _ in range(2))

    def propose(application: EvolutionApplication) -> EvolutionProposal | None:
        return application.propose(
            FailureDiagnosis(stale_results=5),
            reason="concurrent retry diagnosis",
            evidence_ref="artifact://postgres/concurrent/diagnosis",
            idempotency_key="postgres:concurrent:proposal",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        proposals = tuple(executor.map(propose, evolution_apps))
    assert proposals[0] is not None and proposals[1] is not None
    assert proposals[0].candidate == proposals[1].candidate
    assert proposals[0].experiment == proposals[1].experiment
    assert sorted(item.idempotent_replay for item in proposals) == [False, True]

    experiment_id = proposals[0].experiment.id
    clock.advance(seconds=1)

    def advance(application: EvolutionApplication) -> EvolutionTransitionResult:
        return _advance_evolution(
            application,
            store,
            clock,
            experiment_id,
            ExperimentStage.OFFLINE_PASSED,
            reason="concurrent offline retry",
            evidence_ref="artifact://postgres/concurrent/offline",
            idempotency_key="postgres:concurrent:offline",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(advance, evolution_apps))
    assert results[0].experiment == results[1].experiment
    assert sorted(item.idempotent_replay for item in results) == [False, True]
    assert len(store.experiment_transition_history(experiment_id)) == 2

    conninfo = postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(conninfo) as connection:
        assert connection.execute("SELECT count(*) FROM evolution_experiments").fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM evolution_experiment_transitions"
        ).fetchone() == (2,)
        assert connection.execute("SELECT count(*) FROM strategy_snapshots").fetchone() == (2,)
    memory.close()


def test_readyz_recovers_after_postgres_terminates_pooled_connections(
    postgres_url: str,
) -> None:
    settings = Settings(
        store="postgres",
        database_url=postgres_url,
        database_pool_min_size=1,
        database_pool_max_size=2,
    )
    payload = {
        "tenant_id": "tenant-recovery",
        "subject_id": "alice",
        "source": "integration-test",
        "idempotency_key": "recovery:preference",
        "key": "drink.preference",
        "value": "decaf coffee",
        "context": {"time_of_day": "evening"},
        "evidence_text": "I prefer decaf coffee in the evening",
        "confidence": 0.9,
    }

    with TestClient(create_app(settings=settings)) as client:
        assert client.get("/readyz").status_code == 200
        assert client.post("/v1/preferences", json=payload).status_code == 201

        conninfo = postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(conninfo, autocommit=True) as admin:
            terminated = admin.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND usename = current_user
                  AND pid <> pg_backend_pid()
                """
            ).fetchall()
        assert any(row[0] for row in terminated)

        deadline = time.monotonic() + 5
        readiness = client.get("/readyz")
        while readiness.status_code != 200 and time.monotonic() < deadline:
            time.sleep(0.05)
            readiness = client.get("/readyz")

        assert readiness.status_code == 200
        assert readiness.json() == {"status": "ready", "storage": "postgres"}
        listed = client.get(
            "/v1/preferences",
            params={"tenant_id": "tenant-recovery", "subject_id": "alice"},
        )
        assert listed.status_code == 200
        assert [(item["key"], item["value"]) for item in listed.json()] == [
            ("drink.preference", "decaf coffee")
        ]


def test_readyz_fails_fast_during_complete_database_outage_and_recovers(
    postgres_url: str,
) -> None:
    target_host, target_port = postgres_target(postgres_url)
    with TcpFaultProxy(target_host, target_port) as proxy:
        settings = Settings(
            store="postgres",
            database_url=database_url_through_proxy(postgres_url, proxy.port),
            database_pool_min_size=1,
            database_pool_max_size=2,
            database_readiness_timeout_seconds=0.2,
        )
        payload = {
            "tenant_id": "tenant-outage",
            "subject_id": "alice",
            "source": "integration-test",
            "idempotency_key": "outage:preference",
            "key": "drink.preference",
            "value": "herbal tea",
            "context": {"time_of_day": "evening"},
            "evidence_text": "I prefer herbal tea in the evening",
            "confidence": 0.9,
        }

        with TestClient(create_app(settings=settings)) as client:
            assert client.post("/v1/preferences", json=payload).status_code == 201
            proxy.set_available(False)

            started_at = time.monotonic()
            unavailable = client.get("/readyz")
            elapsed = time.monotonic() - started_at

            assert unavailable.status_code == 503
            assert unavailable.json() == {"status": "not_ready", "storage": "postgres"}
            assert elapsed < 2
            assert client.get("/livez").status_code == 200

            proxy.set_available(True)
            deadline = time.monotonic() + 15
            readiness = client.get("/readyz")
            while readiness.status_code != 200 and time.monotonic() < deadline:
                time.sleep(0.1)
                readiness = client.get("/readyz")

            assert readiness.status_code == 200
            listed = client.get(
                "/v1/preferences",
                params={"tenant_id": "tenant-outage", "subject_id": "alice"},
            )
            assert listed.status_code == 200
            assert [(item["key"], item["value"]) for item in listed.json()] == [
                ("drink.preference", "herbal tea")
            ]


def test_postgres_reconstructs_bitemporal_memory_and_historical_utility(
    postgres_url: str,
) -> None:
    scope = Scope("tenant-time", "alice")
    other_scope = Scope("tenant-time", "bob")
    context = ContextSignature.from_mapping({"time_of_day": "evening"})
    clock = FixedClock()
    store = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
    app = MemoryApplication(store=store, clock=clock, ids=SequentialIds())

    original = app.remember_preference(
        replace(
            _preference(scope, context, "time:original"),
            occurred_at=clock.now() - timedelta(days=4),
        )
    )
    known_before_correction = clock.now()
    clock.advance(days=1)
    corrected = app.correct_preference(
        CorrectPreference(
            scope=scope,
            record_id=original.record_id,
            source="profile-reconciliation",
            idempotency_key="time:late-correction",
            value="herbal tea",
            evidence_text="The corrected preference was already effective last week",
            reason="late-arriving correction",
            # Deliberately earlier than the original valid_from. Transaction time,
            # not the greatest valid_from, decides which known correction wins.
            occurred_at=clock.now() - timedelta(days=10),
            expected_revision_id=original.revision_id,
        )
    )
    corrected_revision = app.history(scope, original.record_id)[-1]

    before_known = store.memories_as_of(
        scope,
        valid_at=clock.now(),
        known_at=known_before_correction,
    )
    after_known = store.memories_as_of(
        scope,
        valid_at=clock.now(),
        known_at=clock.now(),
    )
    assert [snapshot.revision.id for snapshot in before_known] == [original.revision_id]
    assert [snapshot.revision.id for snapshot in after_known] == [corrected.revision_id]

    clock.advance(days=1)
    future_valid_at = clock.now() + timedelta(days=30)
    future = app.correct_preference(
        CorrectPreference(
            scope=scope,
            record_id=original.record_id,
            source="explicit-feedback",
            idempotency_key="time:future-correction",
            value="water",
            evidence_text="Starting next month I will prefer water",
            reason="scheduled preference change",
            occurred_at=future_valid_at,
            expected_revision_id=corrected.revision_id,
        )
    )
    current = store.memories_as_of(
        scope,
        valid_at=clock.now(),
        known_at=clock.now(),
    )
    effective_future = store.memories_as_of(
        scope,
        valid_at=future_valid_at,
        known_at=clock.now(),
    )
    assert [snapshot.revision.id for snapshot in current] == [corrected.revision_id]
    assert [snapshot.revision.id for snapshot in effective_future] == [future.revision_id]

    app.remember_preference(
        replace(
            _preference(other_scope, context, "time:other-scope"),
            value="sparkling water",
            evidence_text="I prefer sparkling water",
            occurred_at=clock.now() - timedelta(days=1),
        )
    )
    assert [
        snapshot.revision.id
        for snapshot in store.memories_as_of(
            scope,
            valid_at=clock.now(),
            known_at=clock.now(),
        )
    ] == [corrected.revision_id]

    trace = app.recall(
        RecallMemory(
            scope=scope,
            query="drink preference",
            context=context,
            valid_at=clock.now(),
            known_at=clock.now(),
        )
    )
    assert [item.revision_id for item in trace.items] == [corrected.revision_id]
    assert trace.items[0].revision_valid_from == corrected_revision.valid_from
    assert trace.items[0].revision_recorded_at == corrected_revision.recorded_at

    known_before_outcome = clock.now()
    clock.advance(hours=1)
    outcome = app.record_outcome(
        RecordOutcome(
            scope=scope,
            trace_id=trace.id,
            revision_id=corrected.revision_id,
            kind=OutcomeKind.HELPFUL,
            idempotency_key="time:helpful-outcome",
            occurred_at=clock.now() - timedelta(days=20),
        )
    )
    utility_before = store.utility_for_as_of(
        scope,
        corrected.revision_id,
        context,
        known_at=known_before_outcome,
    )
    utility_after = store.utility_for_as_of(
        scope,
        corrected.revision_id,
        context,
        known_at=outcome.outcome.recorded_at,
    )
    assert utility_before.mean == 0.5
    assert utility_after.mean > 0.5
    assert utility_after.last_outcome_at == outcome.outcome.occurred_at
    assert (
        store.utility_for_as_of(
            other_scope,
            corrected.revision_id,
            context,
            known_at=outcome.outcome.recorded_at,
        ).mean
        == 0.5
    )
    assert store.trace(other_scope, trace.id) is None

    conninfo = postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(conninfo) as connection:
        outbox_row = connection.execute(
            """
            SELECT occurred_at, payload
            FROM outbox_events
            WHERE aggregate_type = 'outcome' AND aggregate_id = %s
            """,
            (outcome.outcome.id,),
        ).fetchone()
    assert outbox_row is not None
    assert outbox_row[0] == outcome.outcome.recorded_at
    assert outbox_row[1]["occurred_at"] == outcome.outcome.occurred_at.isoformat()
    app.close()


def test_bitemporal_migration_downgrades_and_backfills_existing_rows(
    postgres_url: str,
) -> None:
    scope = Scope("tenant-migration", "alice")
    context = ContextSignature.from_mapping({"channel": "assistant"})
    clock = FixedClock()
    store = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
    app = MemoryApplication(store=store, clock=clock, ids=SequentialIds())
    legacy_policy_id = app.retrieval_policy.id
    original = app.remember_preference(_preference(scope, context, "migration:preference"))
    future_valid_at = clock.now() + timedelta(days=30)
    created = app.correct_preference(
        CorrectPreference(
            scope=scope,
            record_id=original.record_id,
            source="explicit-feedback",
            idempotency_key="migration:future-correction",
            value="water",
            evidence_text="Starting next month I prefer water",
            reason="scheduled preference change",
            occurred_at=future_valid_at,
            expected_revision_id=original.revision_id,
        )
    )
    trace = app.recall(
        RecallMemory(
            scope=scope,
            query="drink preference",
            context=context,
            valid_at=future_valid_at,
        )
    )
    outcome = app.record_outcome(
        RecordOutcome(
            scope=scope,
            trace_id=trace.id,
            revision_id=created.revision_id,
            kind=OutcomeKind.ACCEPTED,
            idempotency_key="migration:outcome",
            occurred_at=clock.now() + timedelta(days=60),
        )
    )
    app.close()

    config = alembic_config(postgres_url)
    alembic_command.downgrade(config, "0002_scope_integrity")
    try:
        conninfo = postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(conninfo) as connection:
            columns = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name IN ('recall_traces', 'recall_trace_items', 'outcomes')
                    """
                ).fetchall()
            }
            assert "known_at" not in columns
            assert "revision_valid_from" not in columns
            assert "recorded_at" not in columns
            dropped_indexes = connection.execute(
                """
                SELECT to_regclass('ix_revisions_record_bitemporal'),
                       to_regclass('ix_outcomes_scope_revision_recorded')
                """
            ).fetchone()
            assert dropped_indexes == (None, None)
            assert connection.execute(
                """
                SELECT to_regclass('strategy_activations'),
                       to_regclass('evolution_experiments'),
                       to_regclass('evolution_experiment_transitions')
                """
            ).fetchone() == (None, None, None)

        alembic_command.upgrade(config, "head")
        with psycopg.connect(conninfo, autocommit=True) as connection:
            trace_row = connection.execute(
                """
                SELECT valid_at, known_at, created_at
                FROM recall_traces WHERE id = %s
                """,
                (trace.id,),
            ).fetchone()
            assert trace_row is not None
            assert trace_row[0] == future_valid_at
            assert trace_row[0] > trace_row[2]
            assert trace_row[1] == trace_row[2]

            item_row = connection.execute(
                """
                SELECT item.revision_valid_from, item.revision_recorded_at,
                       revision.valid_from, revision.recorded_at
                FROM recall_trace_items AS item
                JOIN memory_revisions AS revision ON revision.id = item.revision_id
                WHERE item.trace_id = %s
                """,
                (trace.id,),
            ).fetchone()
            assert item_row is not None
            assert item_row[:2] == item_row[2:]

            outcome_row = connection.execute(
                """
                SELECT recorded_at, occurred_at, CURRENT_TIMESTAMP
                FROM outcomes WHERE id = %s
                """,
                (outcome.outcome.id,),
            ).fetchone()
            assert outcome_row is not None
            assert outcome_row[0] < outcome_row[1]
            assert outcome_row[0] <= outcome_row[2]
            indexes = connection.execute(
                """
                SELECT to_regclass('ix_revisions_record_bitemporal'),
                       to_regclass('ix_outcomes_scope_revision_recorded')
                """
            ).fetchone()
            assert indexes == (
                "ix_revisions_record_bitemporal",
                "ix_outcomes_scope_revision_recorded",
            )
            assert connection.execute(
                """
                SELECT to_regclass('strategy_activations'),
                       to_regclass('evolution_experiments'),
                       to_regclass('evolution_experiment_transitions')
                """
            ).fetchone() == (
                "strategy_activations",
                "evolution_experiments",
                "evolution_experiment_transitions",
            )
            assert connection.execute("SELECT count(*) FROM strategy_activations").fetchone() == (
                0,
            )
            assert connection.execute("SELECT count(*) FROM evolution_experiments").fetchone() == (
                0,
            )
            with pytest.raises(CheckViolation):
                connection.execute(
                    """
                    UPDATE recall_traces
                    SET known_at = created_at + interval '1 second'
                    WHERE id = %s
                    """,
                    (trace.id,),
                )

        reopened = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
        migrated_app = MemoryApplication(
            store=reopened,
            clock=clock,
            ids=Uuid4Generator(),
        )
        try:
            assert migrated_app.retrieval_policy.id != legacy_policy_id
            assert len(reopened.strategy_activation_history()) == 1
            migrated_trace = reopened.trace(scope, trace.id)
            assert migrated_trace is not None
            assert migrated_trace.valid_at == future_valid_at
            assert migrated_trace.known_at == migrated_trace.created_at
            assert migrated_trace.items[0].revision_id == created.revision_id
        finally:
            migrated_app.close()
    finally:
        # Keep the shared integration database at head even if an assertion fails.
        alembic_command.upgrade(config, "head")


def test_postgres_rejects_cross_record_and_strategy_attribution(postgres_url: str) -> None:
    scope = Scope("tenant-integrity", "alice")
    context = ContextSignature.from_mapping({"channel": "assistant"})
    clock = FixedClock()
    store = PostgresMemoryStore(postgres_url, min_size=1, max_size=2)
    app = MemoryApplication(store=store, clock=clock, ids=Uuid4Generator())

    first = app.remember_preference(
        replace(
            _preference(scope, context, "integrity:first"),
            key="drink.preference",
            value="tea",
            evidence_text="I prefer tea",
        )
    )
    second = app.remember_preference(
        replace(
            _preference(scope, context, "integrity:second"),
            key="snack.preference",
            value="fruit",
            evidence_text="I prefer fruit",
        )
    )
    corrected = app.correct_preference(
        CorrectPreference(
            scope=scope,
            record_id=first.record_id,
            source="explicit-feedback",
            idempotency_key="integrity:correction",
            value="herbal tea",
            evidence_text="I now prefer herbal tea",
            reason="explicit correction",
            occurred_at=clock.now(),
            expected_revision_id=first.revision_id,
        )
    )
    trace = app.recall(
        RecallMemory(
            scope=scope,
            query="preference",
            context=context,
            limit=5,
        )
    )
    assert {item.revision_id for item in trace.items} == {
        corrected.revision_id,
        second.revision_id,
    }

    conninfo = postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(conninfo, autocommit=True) as connection:
        with pytest.raises(ForeignKeyViolation):
            connection.execute(
                "UPDATE candidates SET accepted_record_id = %s WHERE id = %s",
                (second.record_id, first.candidate_id),
            )
        with pytest.raises(CheckViolation):
            connection.execute(
                "UPDATE candidates SET state = 'proposed' WHERE id = %s",
                (first.candidate_id,),
            )
        with pytest.raises(ForeignKeyViolation):
            connection.execute(
                "UPDATE memory_revisions SET supersedes_revision_id = %s WHERE id = %s",
                (second.revision_id, corrected.revision_id),
            )
        with pytest.raises(ForeignKeyViolation):
            connection.execute(
                """
                UPDATE recall_trace_items SET record_id = %s
                WHERE trace_id = %s AND revision_id = %s
                """,
                (second.record_id, trace.id, corrected.revision_id),
            )
        with pytest.raises(ForeignKeyViolation):
            connection.execute(
                """
                UPDATE recall_trace_items
                SET revision_valid_from = revision_valid_from + interval '1 second'
                WHERE trace_id = %s AND revision_id = %s
                """,
                (trace.id, corrected.revision_id),
            )
        with pytest.raises(ForeignKeyViolation):
            connection.execute(
                "UPDATE recall_traces SET policy_version = 999 WHERE id = %s",
                (trace.id,),
            )
    app.close()
