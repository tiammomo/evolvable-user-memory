from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
    DomainError,
    NotFoundError,
    Scope,
)
from evolvable_memory.domain.evidence import Candidate
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
    source: str = "conversation",
    evidence_text: str | None = None,
    occurred_at: datetime = NOW,
) -> RememberPreference:
    return RememberPreference(
        scope=scope,
        source=source,
        idempotency_key=idempotency_key,
        key=key,
        value=value,
        context=context,
        evidence_text=evidence_text or f"I prefer {value}",
        confidence=confidence,
        occurred_at=occurred_at,
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


def test_in_memory_transaction_rolls_back_a_partially_applied_use_case(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_update_candidate = harness.store.update_candidate

    def fail_after_memory_write(_candidate: Candidate) -> None:
        raise RuntimeError("force rollback after memory write")

    monkeypatch.setattr(harness.store, "update_candidate", fail_after_memory_write)

    with pytest.raises(RuntimeError, match="force rollback"):
        harness.app.remember_preference(preference())

    assert harness.store.observation_count == 0
    assert harness.store.transition_count == 0
    assert harness.app.list_preferences(ALICE) == ()

    monkeypatch.setattr(harness.store, "update_candidate", original_update_candidate)
    recovered = harness.app.remember_preference(preference())
    assert recovered.idempotent_replay is False


def test_in_memory_nested_transaction_rolls_back_only_the_failed_savepoint(
    harness: Harness,
) -> None:
    with harness.store.transaction():
        first = harness.app.remember_preference(preference())
        with (
            pytest.raises(RuntimeError, match="inner rollback"),
            harness.store.transaction(),
        ):
            harness.app.remember_preference(
                preference(
                    idempotency_key="nested:second-evidence",
                    confidence=0.9,
                )
            )
            raise RuntimeError("inner rollback")

        assert [item.id for item in harness.app.history(ALICE, first.record_id)] == [
            first.revision_id
        ]

    assert harness.store.observation_count == 1
    assert harness.store.transition_count == 1


def test_in_memory_outer_transaction_rolls_back_successful_nested_work(
    harness: Harness,
) -> None:
    with pytest.raises(RuntimeError, match="outer rollback"), harness.store.transaction():
        harness.app.remember_preference(preference())
        with harness.store.transaction():
            harness.app.remember_preference(
                preference(idempotency_key="nested:committed-inner", confidence=0.9)
            )
        raise RuntimeError("outer rollback")

    assert harness.store.observation_count == 0
    assert harness.store.transition_count == 0
    assert harness.app.list_preferences(ALICE) == ()


def test_idempotency_key_cannot_hide_different_input(harness: Harness) -> None:
    harness.app.remember_preference(preference())

    with pytest.raises(ConflictError, match="different preference"):
        harness.app.remember_preference(preference(value="tea"))


@pytest.mark.parametrize(
    ("source", "evidence_text", "confidence"),
    [
        ("import", "I prefer decaf coffee", 0.8),
        ("conversation", "Decaf is my evening choice", 0.8),
        ("conversation", "I prefer decaf coffee", 0.9),
    ],
)
def test_idempotency_compares_the_complete_preference_request(
    harness: Harness,
    source: str,
    evidence_text: str,
    confidence: float,
) -> None:
    harness.app.remember_preference(preference())

    with pytest.raises(ConflictError, match="different preference request"):
        harness.app.remember_preference(
            preference(
                source=source,
                evidence_text=evidence_text,
                confidence=confidence,
            )
        )


def test_text_normalization_keeps_idempotent_retries_stable(harness: Harness) -> None:
    first = harness.app.remember_preference(
        preference(
            source=" conversation ",
            idempotency_key=" turn-1:preference-1 ",
            key=" drink.preference ",
            value=" decaf coffee ",
            evidence_text=" I prefer decaf coffee ",
        )
    )
    replay = harness.app.remember_preference(preference())

    assert replay.idempotent_replay is True
    assert replay.revision_id == first.revision_id


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


def test_valid_time_hides_a_future_correction_until_it_becomes_effective(
    harness: Harness,
) -> None:
    original = harness.app.remember_preference(preference(occurred_at=NOW - timedelta(days=1)))
    harness.clock.advance(hours=1)
    future_valid_at = NOW + timedelta(days=30)
    corrected = harness.app.correct_preference(
        CorrectPreference(
            scope=ALICE,
            record_id=original.record_id,
            source="explicit-user-correction",
            idempotency_key="turn-2:future-correction",
            value="herbal tea",
            evidence_text="Starting next month I prefer herbal tea",
            reason="scheduled preference change",
            occurred_at=future_valid_at,
        )
    )

    before_effective = harness.app.recall(
        RecallMemory(scope=ALICE, query="drink preference", context=EVENING)
    )
    after_effective = harness.app.recall(
        RecallMemory(
            scope=ALICE,
            query="drink preference",
            context=EVENING,
            valid_at=future_valid_at,
        )
    )

    assert [item.revision_id for item in before_effective.items] == [original.revision_id]
    assert [item.revision_id for item in after_effective.items] == [corrected.revision_id]
    assert before_effective.items[0].revision_valid_from == NOW - timedelta(days=1)
    assert before_effective.items[0].revision_recorded_at == NOW
    assert after_effective.items[0].revision_valid_from == future_valid_at
    assert after_effective.items[0].revision_recorded_at == harness.clock.current
    assert before_effective.valid_at == harness.clock.current
    assert before_effective.known_at == harness.clock.current
    assert after_effective.valid_at == future_valid_at
    assert after_effective.known_at == harness.clock.current
    assert [item.revision.id for item in harness.app.list_preferences(ALICE)] == [
        original.revision_id
    ]


def test_system_time_excludes_a_late_correction_until_it_was_known(
    harness: Harness,
) -> None:
    original = harness.app.remember_preference(preference(occurred_at=NOW - timedelta(days=30)))
    known_before_correction = harness.clock.current
    harness.clock.advance(days=1)
    corrected = harness.app.correct_preference(
        CorrectPreference(
            scope=ALICE,
            record_id=original.record_id,
            source="profile-reconciliation",
            idempotency_key="turn-2:late-correction",
            value="herbal tea",
            evidence_text="The preference changed three weeks ago",
            reason="late-arriving correction",
            occurred_at=NOW - timedelta(days=21),
        )
    )
    query_valid_at = harness.clock.current

    before_recorded = harness.app.recall(
        RecallMemory(
            scope=ALICE,
            query="drink preference",
            context=EVENING,
            valid_at=query_valid_at,
            known_at=known_before_correction - timedelta(microseconds=1),
        )
    )
    before_known = harness.app.recall(
        RecallMemory(
            scope=ALICE,
            query="drink preference",
            context=EVENING,
            valid_at=query_valid_at,
            known_at=known_before_correction,
        )
    )
    after_known = harness.app.recall(
        RecallMemory(
            scope=ALICE,
            query="drink preference",
            context=EVENING,
            valid_at=query_valid_at,
            known_at=harness.clock.current,
        )
    )

    assert before_recorded.items == ()
    assert [item.revision_id for item in before_known.items] == [original.revision_id]
    assert [item.revision_id for item in after_known.items] == [corrected.revision_id]
    assert before_known.known_at == known_before_correction
    assert after_known.known_at == harness.clock.current


def test_later_recorded_correction_wins_even_with_an_earlier_valid_time(
    harness: Harness,
) -> None:
    original = harness.app.remember_preference(preference(occurred_at=NOW - timedelta(days=10)))
    harness.clock.advance(days=1)
    corrected = harness.app.correct_preference(
        CorrectPreference(
            scope=ALICE,
            record_id=original.record_id,
            source="historical-reconciliation",
            idempotency_key="turn-2:retroactive-correction",
            value="herbal tea",
            evidence_text="The earlier preference should have been herbal tea",
            reason="retroactive correction",
            occurred_at=NOW - timedelta(days=20),
        )
    )

    trace = harness.app.recall(
        RecallMemory(
            scope=ALICE,
            query="drink preference",
            context=EVENING,
            valid_at=NOW,
            known_at=harness.clock.current,
        )
    )

    assert [item.revision_id for item in trace.items] == [corrected.revision_id]


def test_correction_rejects_a_stale_expected_revision_but_allows_its_retry(
    harness: Harness,
) -> None:
    original = harness.app.remember_preference(preference())
    first_correction = CorrectPreference(
        scope=ALICE,
        record_id=original.record_id,
        source="explicit-user-correction",
        idempotency_key="turn-2:correction",
        value="herbal tea",
        evidence_text="I now prefer herbal tea",
        reason="preference changed",
        occurred_at=NOW,
        expected_revision_id=original.revision_id,
    )

    corrected = harness.app.correct_preference(first_correction)
    replay = harness.app.correct_preference(first_correction)

    assert replay.idempotent_replay is True
    assert replay.revision_id == corrected.revision_id

    with pytest.raises(ConflictError, match="different preference request"):
        harness.app.correct_preference(
            CorrectPreference(
                scope=ALICE,
                record_id=original.record_id,
                source="explicit-user-correction",
                idempotency_key="turn-2:correction",
                value="herbal tea",
                evidence_text="I now prefer herbal tea",
                reason="a different reason",
                occurred_at=NOW,
                expected_revision_id=original.revision_id,
            )
        )

    with pytest.raises(ConflictError, match="expected revision is no longer active"):
        harness.app.correct_preference(
            CorrectPreference(
                scope=ALICE,
                record_id=original.record_id,
                source="stale-page",
                idempotency_key="turn-3:stale-correction",
                value="water",
                evidence_text="A stale page submitted water",
                reason="stale edit",
                occurred_at=NOW,
                expected_revision_id=original.revision_id,
            )
        )

    assert len(harness.app.history(ALICE, original.record_id)) == 2
    assert harness.store.observation_count == 2


def test_same_source_does_not_inflate_diversity_or_move_evidence_time_back(
    harness: Harness,
) -> None:
    memory = harness.app.remember_preference(preference(source="conversation"))
    harness.app.remember_preference(
        preference(
            source="conversation",
            idempotency_key="turn-2:same-source",
            occurred_at=NOW - timedelta(days=30),
        )
    )
    harness.app.remember_preference(
        preference(
            source="profile-import",
            idempotency_key="turn-3:new-source",
            occurred_at=NOW - timedelta(days=10),
        )
    )

    latest = harness.app.history(ALICE, memory.record_id)[-1]
    assert latest.belief.source_diversity == 2
    assert latest.belief.source_keys == ("conversation", "profile-import")
    assert latest.belief.last_evidence_at == NOW


def test_scope_isolation_applies_to_read_correction_and_history(harness: Harness) -> None:
    memory = harness.app.remember_preference(preference())
    other_scope = Scope("tenant-b", "alice")

    assert harness.store.candidate_for_observation(ALICE, memory.observation_id) is not None
    assert harness.store.candidate_for_observation(other_scope, memory.observation_id) is None
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
    before_utility = harness.store.utility_for(ALICE, memory.revision_id, EVENING)

    for _ in range(3):
        harness.app.recall(RecallMemory(scope=ALICE, query="decaf coffee", context=EVENING))

    after_history = harness.app.history(ALICE, memory.record_id)
    after_utility = harness.store.utility_for(ALICE, memory.revision_id, EVENING)
    assert after_history == before_history
    assert after_utility == before_utility
    assert before_utility.mean == 0.5
    assert before_utility.sample_weight == 0.0


def test_as_of_recall_is_scope_local_read_only_and_reproducible(
    harness: Harness,
) -> None:
    memory = harness.app.remember_preference(preference())
    valid_at = harness.clock.current
    known_at = harness.clock.current
    before_history = harness.app.history(ALICE, memory.record_id)
    before_utility = harness.store.utility_for(ALICE, memory.revision_id, EVENING)

    first = harness.app.recall(
        RecallMemory(
            scope=ALICE,
            query="decaf coffee",
            context=EVENING,
            valid_at=valid_at,
            known_at=known_at,
        )
    )
    cross_scope = harness.app.recall(
        RecallMemory(
            scope=Scope("tenant-b", "alice"),
            query="decaf coffee",
            context=EVENING,
            valid_at=valid_at,
            known_at=known_at,
        )
    )
    harness.clock.advance(days=365)
    replay = harness.app.recall(
        RecallMemory(
            scope=ALICE,
            query="decaf coffee",
            context=EVENING,
            valid_at=valid_at,
            known_at=known_at,
        )
    )

    assert cross_scope.items == ()
    assert [item.revision_id for item in first.items] == [memory.revision_id]
    assert replay.items == first.items
    assert replay.valid_at == first.valid_at == valid_at
    assert replay.known_at == first.known_at == known_at
    assert harness.app.history(ALICE, memory.record_id) == before_history
    assert harness.store.utility_for(ALICE, memory.revision_id, EVENING) == before_utility


def test_as_of_recall_uses_only_outcomes_known_at_the_query_time(
    harness: Harness,
) -> None:
    memory = harness.app.remember_preference(preference())
    before_outcome = harness.app.recall(
        RecallMemory(scope=ALICE, query="decaf coffee", context=EVENING)
    )
    harness.clock.advance(days=1)
    harness.app.record_outcome(
        RecordOutcome(
            scope=ALICE,
            trace_id=before_outcome.id,
            revision_id=memory.revision_id,
            kind=OutcomeKind.HELPFUL,
            idempotency_key="task-10:outcome-1",
            occurred_at=harness.clock.current,
        )
    )

    historical = harness.app.recall(
        RecallMemory(
            scope=ALICE,
            query="decaf coffee",
            context=EVENING,
            valid_at=harness.clock.current,
            known_at=before_outcome.known_at,
        )
    )
    current = harness.app.recall(RecallMemory(scope=ALICE, query="decaf coffee", context=EVENING))

    assert historical.items[0].breakdown.utility == 0.5
    assert current.items[0].breakdown.utility == pytest.approx(2 / 3)
    assert harness.app.history(ALICE, memory.record_id)[-1].id == memory.revision_id


def test_recall_rejects_invalid_as_of_times(harness: Harness) -> None:
    naive = datetime(2026, 7, 14, 4, 0)
    with pytest.raises(DomainError, match="valid_at must be timezone-aware"):
        RecallMemory(
            scope=ALICE,
            query="drink",
            context=EVENING,
            valid_at=naive,
        )
    with pytest.raises(DomainError, match="known_at must be timezone-aware"):
        RecallMemory(
            scope=ALICE,
            query="drink",
            context=EVENING,
            known_at=naive,
        )
    with pytest.raises(DomainError, match="known_at must not be in the future"):
        harness.app.recall(
            RecallMemory(
                scope=ALICE,
                query="drink",
                context=EVENING,
                known_at=harness.clock.current + timedelta(microseconds=1),
            )
        )


def test_recall_abstains_without_lexical_or_explicit_context_relevance(
    harness: Harness,
) -> None:
    memory = harness.app.remember_preference(preference())
    before_history = harness.app.history(ALICE, memory.record_id)
    before_utility = harness.store.utility_for(ALICE, memory.revision_id, EVENING)

    trace = harness.app.recall(
        RecallMemory(
            scope=ALICE,
            query="preferred code editor theme",
            context=ContextSignature(),
        )
    )

    assert trace.items == ()
    assert harness.app.history(ALICE, memory.record_id) == before_history
    assert harness.store.utility_for(ALICE, memory.revision_id, EVENING) == before_utility


def test_explicit_context_can_bridge_query_and_memory_vocabulary(harness: Harness) -> None:
    memory = harness.app.remember_preference(preference())

    trace = harness.app.recall(
        RecallMemory(scope=ALICE, query="晚上应该准备什么饮料", context=EVENING)
    )

    assert [item.record_id for item in trace.items] == [memory.record_id]


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
    assert first.outcome.recorded_at == harness.clock.current
    assert first.utility.mean == pytest.approx(2 / 3)
    assert replay.utility == first.utility
    assert harness.store.outcome_count == 1


def test_out_of_order_outcomes_keep_current_utility_time_monotonic(
    harness: Harness,
) -> None:
    memory = harness.app.remember_preference(preference())
    trace = harness.app.recall(RecallMemory(scope=ALICE, query="decaf coffee", context=EVENING))
    harness.clock.advance(days=2)
    latest_outcome_at = harness.clock.now()
    harness.app.record_outcome(
        RecordOutcome(
            scope=ALICE,
            trace_id=trace.id,
            revision_id=memory.revision_id,
            kind=OutcomeKind.HELPFUL,
            idempotency_key="outcome:latest-business-time",
            occurred_at=latest_outcome_at,
        )
    )
    harness.clock.advance(seconds=1)
    late_arrival = harness.app.record_outcome(
        RecordOutcome(
            scope=ALICE,
            trace_id=trace.id,
            revision_id=memory.revision_id,
            kind=OutcomeKind.HARMFUL,
            idempotency_key="outcome:late-arrival",
            occurred_at=latest_outcome_at - timedelta(days=1),
        )
    )

    historical = harness.store.utility_for_as_of(
        ALICE,
        memory.revision_id,
        EVENING,
        known_at=harness.clock.now(),
    )
    assert late_arrival.utility.last_outcome_at == latest_outcome_at
    assert historical.last_outcome_at == latest_outcome_at
    assert late_arrival.utility == historical


def test_outcome_idempotency_normalizes_text_and_rejects_a_changed_note(
    harness: Harness,
) -> None:
    memory = harness.app.remember_preference(preference())
    trace = harness.app.recall(RecallMemory(scope=ALICE, query="decaf coffee", context=EVENING))
    first = harness.app.record_outcome(
        RecordOutcome(
            scope=ALICE,
            trace_id=trace.id,
            revision_id=memory.revision_id,
            kind=OutcomeKind.HELPFUL,
            idempotency_key=" outcome-1 ",
            occurred_at=NOW,
            note=" accepted ",
        )
    )
    replay = harness.app.record_outcome(
        RecordOutcome(
            scope=ALICE,
            trace_id=trace.id,
            revision_id=memory.revision_id,
            kind=OutcomeKind.HELPFUL,
            idempotency_key="outcome-1",
            occurred_at=NOW + timedelta(seconds=1),
            note="accepted",
        )
    )

    assert first.idempotent_replay is False
    assert replay.idempotent_replay is True

    with pytest.raises(ConflictError, match="different data"):
        harness.app.record_outcome(
            RecordOutcome(
                scope=ALICE,
                trace_id=trace.id,
                revision_id=memory.revision_id,
                kind=OutcomeKind.HELPFUL,
                idempotency_key="outcome-1",
                occurred_at=NOW,
                note="rejected later",
            )
        )


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
    assert harness.store.utility_for(ALICE, memory.revision_id, morning).mean == 0.5
    assert (
        harness.store.utility_for(
            Scope("tenant-b", "alice"),
            memory.revision_id,
            EVENING,
        ).mean
        == 0.5
    )
