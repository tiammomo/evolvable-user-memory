from __future__ import annotations

import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime

import psycopg
import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation

from conftest import FixedClock, SequentialIds
from evolvable_memory.adapters.postgres import PostgresMemoryStore
from evolvable_memory.adapters.system import Uuid4Generator
from evolvable_memory.application.commands import (
    CorrectPreference,
    RecallMemory,
    RecordOutcome,
    RememberPreference,
)
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.domain.common import ConflictError, ContextSignature, Scope
from evolvable_memory.domain.experience import OutcomeKind
from evolvable_memory.migrate import upgrade_database

pytestmark = pytest.mark.postgres


@pytest.fixture
def postgres_url() -> Iterator[str]:
    database_url = os.getenv("EMF_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set EMF_TEST_DATABASE_URL to run PostgreSQL integration tests")

    upgrade_database(database_url)
    conninfo = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(conninfo) as connection:
        connection.execute(
            """
            TRUNCATE TABLE
                outbox_events, utility_estimates, outcomes, recall_trace_items,
                recall_traces, revision_transitions, candidates, memory_revisions,
                memory_records, evidence_spans, observations, strategy_snapshots
            CASCADE
            """
        )
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

    assert app.is_ready()
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

    outcome = app.record_outcome(
        RecordOutcome(
            scope=scope,
            trace_id=trace.id,
            revision_id=created.revision_id,
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
    snapshots = reopened.list_preferences(scope)
    assert [(item.revision.value, item.revision.sequence) for item in snapshots] == [
        ("herbal tea", 2)
    ]

    conninfo = postgres_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(conninfo) as connection:
        outbox_count = connection.execute("SELECT count(*) FROM outbox_events").fetchone()
        assert outbox_count is not None and outbox_count[0] >= 4
    reopened.close()
    assert not reopened.is_ready()


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
                "UPDATE recall_traces SET policy_version = 999 WHERE id = %s",
                (trace.id,),
            )
    app.close()
