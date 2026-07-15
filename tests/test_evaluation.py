from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from conftest import Harness
from evolvable_memory.application.commands import (
    CorrectPreference,
    PreferenceResult,
    RecallMemory,
    RememberPreference,
)
from evolvable_memory.application.evaluation import (
    CorrectionReplayCase,
    DeterministicReplayEvaluator,
    EvaluationDataset,
    HardGatePolicy,
    MemoryReference,
    RecallReplayCase,
    WriteReplayCase,
)
from evolvable_memory.domain.common import ContextSignature, DomainError, Scope
from evolvable_memory.domain.experience import RecalledItem, RecallTrace, ScoreBreakdown

NOW = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)
ALICE = Scope("tenant-a", "alice")
BOB = Scope("tenant-a", "bob")
EVENING = ContextSignature.from_mapping({"time_of_day": "evening"})


def _write(
    case_id: str,
    *,
    scope: Scope = ALICE,
    key: str = "drink.preference",
    value: str = "decaf coffee",
    context: ContextSignature = EVENING,
    idempotency_key: str | None = None,
    expected_sequence: int | None = None,
    expected_idempotent_replay: bool | None = None,
) -> WriteReplayCase:
    return WriteReplayCase(
        case_id=case_id,
        command=RememberPreference(
            scope=scope,
            source="evaluation-fixture",
            idempotency_key=idempotency_key or f"{case_id}:write",
            key=key,
            value=value,
            context=context,
            evidence_text=f"The user stated {value}",
            confidence=0.9,
            occurred_at=NOW,
        ),
        expected_sequence=expected_sequence,
        expected_idempotent_replay=expected_idempotent_replay,
    )


def _correction(
    case_id: str,
    target_case_id: str,
    *,
    scope: Scope = ALICE,
    idempotency_key: str | None = None,
    expected_sequence: int | None = 2,
    expected_idempotent_replay: bool | None = False,
) -> CorrectionReplayCase:
    return CorrectionReplayCase(
        case_id=case_id,
        target=MemoryReference.from_case(target_case_id),
        scope=scope,
        source="explicit-user-correction",
        idempotency_key=idempotency_key or f"{case_id}:correction",
        value="herbal tea",
        evidence_text="The user corrected the preference",
        reason="preference changed",
        occurred_at=NOW,
        expected_sequence=expected_sequence,
        expected_idempotent_replay=expected_idempotent_replay,
    )


def test_dataset_snapshot_hash_is_canonical_and_content_addressed() -> None:
    first = EvaluationDataset(
        name=" smoke ",
        version=" v1 ",
        recall_k=1,
        cases=(
            _write(
                "write",
                context=ContextSignature.from_mapping({"time": "evening", "place": "home"}),
            ),
        ),
    )
    equivalent = EvaluationDataset(
        name="smoke",
        version="v1",
        recall_k=1,
        cases=(
            _write(
                "write",
                context=ContextSignature.from_mapping({"place": "home", "time": "evening"}),
            ),
        ),
    )
    changed = EvaluationDataset(
        name="smoke",
        version="v2",
        recall_k=1,
        cases=equivalent.cases,
    )

    assert first.snapshot_hash == equivalent.snapshot_hash
    assert len(first.snapshot_hash) == 64
    assert changed.snapshot_hash != first.snapshot_hash


def test_dataset_rejects_ambiguous_or_unreplayable_cases() -> None:
    with pytest.raises(DomainError, match="unavailable memory case"):
        EvaluationDataset(
            name="invalid",
            version="v1",
            recall_k=1,
            cases=(_correction("correction", "future"),),
        )

    write = _write("same")
    with pytest.raises(DomainError, match="case_id values must be unique"):
        EvaluationDataset(
            name="invalid",
            version="v1",
            recall_k=1,
            cases=(write, write),
        )

    reference = MemoryReference.from_case("write")
    with pytest.raises(DomainError, match="relevant and forbidden"):
        RecallReplayCase(
            case_id="recall",
            command=RecallMemory(scope=ALICE, query="drink", context=EVENING),
            relevant=(reference,),
            forbidden=(reference,),
        )

    with pytest.raises(DomainError, match="at least recall_k"):
        EvaluationDataset(
            name="invalid",
            version="v1",
            recall_k=2,
            cases=(
                write,
                RecallReplayCase(
                    case_id="recall",
                    command=RecallMemory(
                        scope=ALICE,
                        query="drink",
                        context=EVENING,
                        limit=1,
                    ),
                    relevant=(reference,),
                ),
            ),
        )


def test_replay_exercises_write_and_correction_idempotency(harness: Harness) -> None:
    dataset = EvaluationDataset(
        name="idempotency",
        version="v1",
        recall_k=1,
        cases=(
            _write(
                "write",
                idempotency_key="stable-write",
                expected_sequence=1,
                expected_idempotent_replay=False,
            ),
            _write(
                "write-retry",
                idempotency_key="stable-write",
                expected_sequence=1,
                expected_idempotent_replay=True,
            ),
            _correction(
                "correction",
                "write",
                idempotency_key="stable-correction",
                expected_idempotent_replay=False,
            ),
            _correction(
                "correction-retry",
                "write",
                idempotency_key="stable-correction",
                expected_idempotent_replay=True,
            ),
        ),
    )

    report = DeterministicReplayEvaluator(harness.app).evaluate(
        dataset,
        HardGatePolicy(min_update_accuracy=1.0),
    )

    assert all(case.passed for case in report.cases)
    assert report.cases[0].revision_id == report.cases[1].revision_id
    assert report.cases[2].revision_id == report.cases[3].revision_id
    assert report.metrics.update_accuracy == 1.0
    assert report.gates.passed is True


def test_ordered_replay_runs_write_correction_recall_and_hard_gates(
    harness: Harness,
) -> None:
    dataset = EvaluationDataset(
        name="preference-lifecycle",
        version="v1",
        recall_k=1,
        cases=(
            _write("original", expected_sequence=1, expected_idempotent_replay=False),
            _correction("corrected", "original"),
            _write(
                "forbidden",
                key="private.identifier",
                value="sensitive marker",
                expected_sequence=1,
                expected_idempotent_replay=False,
            ),
            RecallReplayCase(
                case_id="recall-current",
                command=RecallMemory(
                    scope=ALICE,
                    query="herbal tea drink preference",
                    context=EVENING,
                    limit=10,
                ),
                relevant=(MemoryReference.from_case("corrected"),),
                forbidden=(MemoryReference.from_case("forbidden"),),
            ),
            RecallReplayCase(
                case_id="abstain-other-subject",
                command=RecallMemory(
                    scope=BOB,
                    query="unknown preference",
                    context=EVENING,
                    limit=10,
                ),
                expect_abstention=True,
            ),
        ),
    )

    report = DeterministicReplayEvaluator(harness.app).evaluate(
        dataset,
        HardGatePolicy(
            min_recall_at_k=1.0,
            min_mrr=1.0,
            min_update_accuracy=1.0,
            min_abstention_accuracy=1.0,
        ),
    )

    assert report.dataset_snapshot_hash == dataset.snapshot_hash
    assert report.metrics.write_case_count == 2
    assert report.metrics.correction_case_count == 1
    assert report.metrics.recall_case_count == 2
    assert report.metrics.execution_failure_count == 0
    assert report.metrics.recall_at_k == 1.0
    assert report.metrics.mrr_at_k == 1.0
    assert report.metrics.update_accuracy == 1.0
    assert report.metrics.abstention_accuracy == 1.0
    assert report.metrics.forbidden_hit_count == 1
    assert report.gates.passed is False
    assert report.gates.violations == ("forbidden_hits",)


def test_update_accuracy_only_uses_corrections_with_expectations(harness: Harness) -> None:
    dataset = EvaluationDataset(
        name="update-quality",
        version="v1",
        recall_k=1,
        cases=(
            _write("original"),
            _correction("wrong-expectation", "original", expected_sequence=3),
        ),
    )

    report = DeterministicReplayEvaluator(harness.app).evaluate(
        dataset,
        HardGatePolicy(min_update_accuracy=1.0),
    )

    assert report.metrics.update_accuracy == 0.0
    assert report.metrics.execution_failure_count == 1
    assert report.cases[1].error == "expected sequence 3, received 2"
    assert report.gates.violations == ("execution_failures", "update_accuracy")


def test_correction_replay_keeps_the_explicit_scope_boundary(harness: Harness) -> None:
    dataset = EvaluationDataset(
        name="correction-scope-isolation",
        version="v1",
        recall_k=1,
        cases=(
            _write("alice-memory"),
            _correction("bob-correction", "alice-memory", scope=BOB),
        ),
    )

    report = DeterministicReplayEvaluator(harness.app).evaluate(
        dataset,
        HardGatePolicy(min_update_accuracy=1.0),
    )

    assert report.cases[1].passed is False
    assert report.cases[1].error == "NotFoundError"
    assert report.metrics.execution_failure_count == 1
    assert report.metrics.update_accuracy == 0.0
    assert report.gates.violations == ("execution_failures", "update_accuracy")


def test_recall_metrics_use_cutoff_but_forbidden_gate_checks_full_trace() -> None:
    record_ids = tuple(UUID(int=value) for value in (1, 2, 3))
    revision_ids = tuple(UUID(int=value) for value in (11, 12, 13))
    trace = _trace(revision_ids)
    application = _ScriptedRecallApplication({"ranked": trace})
    dataset = EvaluationDataset(
        name="ranking",
        version="v1",
        recall_k=2,
        cases=(
            RecallReplayCase(
                case_id="ranked",
                command=RecallMemory(
                    scope=ALICE,
                    query="ranked",
                    context=EVENING,
                    limit=3,
                ),
                relevant=(
                    MemoryReference.from_ids(record_ids[1], revision_ids[1]),
                    MemoryReference.from_ids(record_ids[2], revision_ids[2]),
                ),
                forbidden=(MemoryReference.from_ids(record_ids[0], revision_ids[0]),),
            ),
        ),
    )

    report = DeterministicReplayEvaluator(application).evaluate(dataset)

    assert report.metrics.recall_at_k == 0.5
    assert report.metrics.mrr_at_k == 0.5
    assert report.metrics.forbidden_hit_count == 1
    assert report.gates.violations == ("forbidden_hits",)


def test_recall_labels_match_the_record_and_revision_pair() -> None:
    record_ids = tuple(UUID(int=value) for value in (1, 2))
    revision_ids = tuple(UUID(int=value) for value in (11, 12))
    dataset = EvaluationDataset(
        name="memory-identity",
        version="v1",
        recall_k=2,
        cases=(
            RecallReplayCase(
                case_id="mismatched-pairs",
                command=RecallMemory(
                    scope=ALICE,
                    query="ranked",
                    context=EVENING,
                    limit=2,
                ),
                relevant=(MemoryReference.from_ids(record_ids[0], revision_ids[1]),),
                forbidden=(MemoryReference.from_ids(record_ids[1], revision_ids[0]),),
            ),
        ),
    )

    report = DeterministicReplayEvaluator(
        _ScriptedRecallApplication({"ranked": _trace(revision_ids)})
    ).evaluate(dataset)

    assert report.metrics.recall_at_k == 0.0
    assert report.metrics.mrr_at_k == 0.0
    assert report.metrics.forbidden_hit_count == 0
    assert report.gates.passed is True


def test_replay_report_does_not_expose_exception_or_command_content() -> None:
    secret = "raw-evidence-must-not-appear"
    case = _write("failure", value=secret)
    report = DeterministicReplayEvaluator(_ExplodingApplication(secret)).evaluate(
        EvaluationDataset(name="privacy", version="v1", recall_k=1, cases=(case,))
    )

    assert report.cases[0].error == "RuntimeError"
    assert secret not in repr(report)


def test_evaluation_recall_does_not_mutate_belief_or_utility(harness: Harness) -> None:
    created = harness.app.remember_preference(_write("seed").command)
    before_history = harness.app.history(ALICE, created.record_id)
    before_utility = harness.store.utility_for(ALICE, created.revision_id, EVENING)
    dataset = EvaluationDataset(
        name="recall-neutrality",
        version="v1",
        recall_k=1,
        cases=(
            RecallReplayCase(
                case_id="recall",
                command=RecallMemory(
                    scope=ALICE,
                    query="decaf coffee",
                    context=EVENING,
                    limit=1,
                ),
                relevant=(MemoryReference.from_ids(created.record_id, created.revision_id),),
            ),
        ),
    )

    report = DeterministicReplayEvaluator(harness.app).evaluate(dataset)

    assert report.metrics.recall_at_k == 1.0
    assert harness.app.history(ALICE, created.record_id) == before_history
    assert harness.store.utility_for(ALICE, created.revision_id, EVENING) == before_utility


def _trace(revision_ids: tuple[UUID, ...]) -> RecallTrace:
    return RecallTrace(
        id=UUID(int=100),
        scope=ALICE,
        query="ranked",
        context=EVENING,
        policy_id=UUID(int=101),
        policy_version=1,
        items=tuple(
            RecalledItem(
                record_id=UUID(int=index),
                revision_id=revision_id,
                key=f"memory.{index}",
                value=f"value {index}",
                context=EVENING,
                revision_valid_from=NOW,
                revision_recorded_at=NOW,
                rank=index,
                score=1.0 / index,
                breakdown=ScoreBreakdown(
                    semantic=0.5,
                    context=1.0,
                    belief=0.8,
                    utility=0.5,
                    recency=1.0,
                ),
                evidence_ids=(UUID(int=200 + index),),
            )
            for index, revision_id in enumerate(revision_ids, start=1)
        ),
        valid_at=NOW,
        known_at=NOW,
        created_at=NOW,
    )


class _ScriptedRecallApplication:
    def __init__(self, traces: dict[str, RecallTrace]) -> None:
        self._traces = traces

    def remember_preference(self, command: RememberPreference) -> PreferenceResult:
        del command
        raise AssertionError("unexpected write")

    def correct_preference(self, command: CorrectPreference) -> PreferenceResult:
        del command
        raise AssertionError("unexpected correction")

    def recall(self, command: RecallMemory) -> RecallTrace:
        return self._traces[command.query]


class _ExplodingApplication:
    def __init__(self, secret: str) -> None:
        self._secret = secret

    def remember_preference(self, command: RememberPreference) -> PreferenceResult:
        del command
        raise RuntimeError(self._secret)

    def correct_preference(self, command: CorrectPreference) -> PreferenceResult:
        del command
        raise RuntimeError(self._secret)

    def recall(self, command: RecallMemory) -> RecallTrace:
        del command
        raise RuntimeError(self._secret)
