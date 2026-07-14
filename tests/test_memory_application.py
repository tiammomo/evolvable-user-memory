from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from conftest import Harness
from evolvable_memory.application.commands import (
    CorrectPreference,
    RecallMemory,
    RecordOutcome,
    RememberPreference,
)
from evolvable_memory.domain.common import (
    AttributionError,
    ConflictError,
    ContextSignature,
    NotFoundError,
    Scope,
)
from evolvable_memory.domain.experience import OutcomeKind

NOW = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
ALICE = Scope("tenant-a", "alice")
EVENING = ContextSignature.from_mapping({"time_of_day": "evening"})


def preference(
    *,
    scope: Scope = ALICE,
    key: str = "drink.preference",
    value: str = "decaf coffee",
    context: ContextSignature = EVENING,
    idempotency_key: str = "turn-1:preference-1",
    confidence: float = 0.8,
) -> RememberPreference:
    return RememberPreference(
        scope=scope,
        source="conversation",
        idempotency_key=idempotency_key,
        key=key,
        value=value,
        context=context,
        evidence_text=f"I prefer {value}",
        confidence=confidence,
        occurred_at=NOW,
    )


def test_preference_ingestion_is_idempotent(harness: Harness) -> None:
    first = harness.app.remember_preference(preference())
    replay = harness.app.remember_preference(preference())

    assert replay.idempotent_replay is True
    assert replay.observation_id == first.observation_id
    assert replay.record_id == first.record_id
    assert replay.revision_id == first.revision_id
    assert harness.store.observation_count == 1
    assert harness.store.transition_count == 1


def test_idempotency_key_cannot_hide_different_input(harness: Harness) -> None:
    harness.app.remember_preference(preference())

    with pytest.raises(ConflictError, match="different preference"):
        harness.app.remember_preference(preference(value="tea"))


def test_contextual_preferences_coexist(harness: Harness) -> None:
    evening = harness.app.remember_preference(preference())
    morning_context = ContextSignature.from_mapping({"time_of_day": "morning"})
    morning = harness.app.remember_preference(
        preference(
            value="espresso",
            context=morning_context,
            idempotency_key="turn-2:preference-1",
        )
    )

    trace = harness.app.recall(
        RecallMemory(
            scope=ALICE,
            query="drink preference espresso coffee",
            context=morning_context,
            limit=10,
        )
    )

    assert morning.record_id != evening.record_id
    assert len(trace.items) == 2
    assert trace.items[0].record_id == morning.record_id
    assert trace.items[0].breakdown.context == 1.0
    assert trace.items[1].breakdown.context == 0.0


def test_repeated_evidence_appends_revision_and_strengthens_belief(harness: Harness) -> None:
    first = harness.app.remember_preference(preference(confidence=0.6))
    second = harness.app.remember_preference(
        preference(idempotency_key="turn-2:preference-1", confidence=0.7)
    )
    history = harness.app.history(ALICE, first.record_id)

    assert second.record_id == first.record_id
    assert second.sequence == 2
    assert len(history) == 2
    assert history[0].value == history[1].value
    assert history[1].supersedes_revision_id == history[0].id
    assert history[1].belief.support_count == 2
    assert history[1].belief.confidence > history[0].belief.confidence
    assert len(history[1].evidence_ids) == 2


def test_correction_preserves_history_and_recall_uses_new_head(harness: Harness) -> None:
    original = harness.app.remember_preference(preference())
    corrected = harness.app.correct_preference(
        CorrectPreference(
            scope=ALICE,
            record_id=original.record_id,
            source="explicit-user-correction",
            idempotency_key="turn-3:correction-1",
            value="herbal tea",
            evidence_text="Correction: in the evening I prefer herbal tea",
            reason="user corrected an outdated preference",
            occurred_at=NOW,
        )
    )

    history = harness.app.history(ALICE, original.record_id)
    trace = harness.app.recall(
        RecallMemory(scope=ALICE, query="evening drink tea", context=EVENING)
    )

    assert corrected.sequence == 2
    assert [item.value for item in history] == ["decaf coffee", "herbal tea"]
    assert history[1].supersedes_revision_id == history[0].id
    assert [item.revision_id for item in trace.items] == [corrected.revision_id]


def test_scope_isolation_applies_to_read_correction_and_history(harness: Harness) -> None:
    memory = harness.app.remember_preference(preference())
    other_scope = Scope("tenant-b", "alice")

    trace = harness.app.recall(RecallMemory(scope=other_scope, query="drink", context=EVENING))
    assert trace.items == ()

    with pytest.raises(NotFoundError):
        harness.app.history(other_scope, memory.record_id)
    with pytest.raises(NotFoundError):
        harness.app.correct_preference(
            CorrectPreference(
                scope=other_scope,
                record_id=memory.record_id,
                source="test",
                idempotency_key="other:correction",
                value="water",
                evidence_text="I prefer water",
                reason="test",
                occurred_at=NOW,
            )
        )


def test_preference_listing_is_current_ordered_and_scope_local(harness: Harness) -> None:
    drink = harness.app.remember_preference(preference())
    harness.app.remember_preference(
        preference(
            key="accessibility.font_size",
            value="large",
            idempotency_key="turn-2:font-size",
        )
    )
    harness.app.correct_preference(
        CorrectPreference(
            scope=ALICE,
            record_id=drink.record_id,
            source="explicit-user-correction",
            idempotency_key="turn-3:correction",
            value="herbal tea",
            evidence_text="I now prefer herbal tea",
            reason="preference changed",
            occurred_at=NOW,
        )
    )
    harness.app.remember_preference(
        preference(
            scope=Scope("tenant-b", "alice"),
            value="water",
            idempotency_key="other-tenant:drink",
        )
    )

    listed = harness.app.list_preferences(ALICE)

    assert [snapshot.record.key for snapshot in listed] == [
        "accessibility.font_size",
        "drink.preference",
    ]
    assert [snapshot.revision.value for snapshot in listed] == ["large", "herbal tea"]
    assert [snapshot.revision.sequence for snapshot in listed] == [1, 2]


def test_recall_does_not_reinforce_belief_or_utility(harness: Harness) -> None:
    memory = harness.app.remember_preference(preference())
    before_history = harness.app.history(ALICE, memory.record_id)
    before_utility = harness.store.utility_for(memory.revision_id, EVENING)

    for _ in range(3):
        harness.app.recall(RecallMemory(scope=ALICE, query="decaf coffee", context=EVENING))

    after_history = harness.app.history(ALICE, memory.record_id)
    after_utility = harness.store.utility_for(memory.revision_id, EVENING)
    assert after_history == before_history
    assert after_utility == before_utility
    assert before_utility.mean == 0.5
    assert before_utility.sample_weight == 0.0


def test_attributable_outcome_updates_contextual_utility_once(harness: Harness) -> None:
    memory = harness.app.remember_preference(preference())
    trace = harness.app.recall(RecallMemory(scope=ALICE, query="decaf coffee", context=EVENING))
    command = RecordOutcome(
        scope=ALICE,
        trace_id=trace.id,
        revision_id=memory.revision_id,
        kind=OutcomeKind.HELPFUL,
        idempotency_key="task-9:outcome-1",
        occurred_at=NOW,
    )

    first = harness.app.record_outcome(command)
    replay = harness.app.record_outcome(command)

    assert first.idempotent_replay is False
    assert replay.idempotent_replay is True
    assert first.utility.mean == pytest.approx(2 / 3)
    assert replay.utility == first.utility
    assert harness.store.outcome_count == 1


def test_outcome_requires_trace_membership_and_scope(harness: Harness) -> None:
    harness.app.remember_preference(preference())
    trace = harness.app.recall(RecallMemory(scope=ALICE, query="decaf coffee", context=EVENING))

    with pytest.raises(AttributionError):
        harness.app.record_outcome(
            RecordOutcome(
                scope=ALICE,
                trace_id=trace.id,
                revision_id=uuid4(),
                kind=OutcomeKind.HELPFUL,
                idempotency_key="invalid-attribution",
                occurred_at=NOW,
            )
        )
    with pytest.raises(NotFoundError):
        harness.app.record_outcome(
            RecordOutcome(
                scope=Scope("other", "alice"),
                trace_id=trace.id,
                revision_id=trace.items[0].revision_id,
                kind=OutcomeKind.HELPFUL,
                idempotency_key="cross-scope",
                occurred_at=NOW,
            )
        )


def test_negative_outcome_reduces_only_matching_context_utility(harness: Harness) -> None:
    memory = harness.app.remember_preference(preference())
    trace = harness.app.recall(RecallMemory(scope=ALICE, query="drink", context=EVENING))
    outcome = harness.app.record_outcome(
        RecordOutcome(
            scope=ALICE,
            trace_id=trace.id,
            revision_id=memory.revision_id,
            kind=OutcomeKind.HARMFUL,
            idempotency_key="harmful-1",
            occurred_at=NOW,
            weight=2.0,
        )
    )

    morning = ContextSignature.from_mapping({"time_of_day": "morning"})
    assert outcome.utility.mean == 0.25
    assert harness.store.utility_for(memory.revision_id, morning).mean == 0.5
