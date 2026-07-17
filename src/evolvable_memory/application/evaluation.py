from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from math import isclose
from typing import Protocol
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
from evolvable_memory.domain.common import (
    ContextSignature,
    DomainError,
    Scope,
    require_text,
    require_utc,
)
from evolvable_memory.domain.experience import OutcomeKind


class ReplayCaseKind(StrEnum):
    WRITE = "write"
    CORRECTION = "correction"
    RECALL = "recall"
    OUTCOME = "outcome"


class ExpectedRecallRejection(StrEnum):
    FUTURE_KNOWN_AT = "future_known_at"


@dataclass(frozen=True, slots=True)
class MemoryReference:
    """A memory created earlier in a replay or already present in a snapshot."""

    case_id: str | None = None
    record_id: UUID | None = None
    revision_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.case_id is not None:
            normalized = require_text(self.case_id, "memory reference case_id")
            if self.record_id is not None or self.revision_id is not None:
                raise DomainError("case memory reference cannot also contain UUIDs")
            object.__setattr__(self, "case_id", normalized)
            return
        if self.record_id is None or self.revision_id is None:
            raise DomainError("direct memory reference requires record_id and revision_id")

    @classmethod
    def from_case(cls, case_id: str) -> MemoryReference:
        return cls(case_id=case_id)

    @classmethod
    def from_ids(cls, record_id: UUID, revision_id: UUID) -> MemoryReference:
        return cls(record_id=record_id, revision_id=revision_id)


@dataclass(frozen=True, slots=True)
class WriteReplayCase:
    case_id: str
    command: RememberPreference
    expected_sequence: int | None = None
    expected_idempotent_replay: bool | None = None
    run_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", require_text(self.case_id, "case_id"))
        _validate_expected_sequence(self.expected_sequence)
        _normalize_run_at(self)


@dataclass(frozen=True, slots=True)
class CorrectionReplayCase:
    case_id: str
    target: MemoryReference
    scope: Scope
    source: str
    idempotency_key: str
    value: str
    evidence_text: str
    reason: str
    occurred_at: datetime
    enforce_expected_revision: bool = True
    expected_sequence: int | None = None
    expected_idempotent_replay: bool | None = None
    run_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", require_text(self.case_id, "case_id"))
        object.__setattr__(self, "source", require_text(self.source, "source"))
        object.__setattr__(
            self,
            "idempotency_key",
            require_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(self, "value", require_text(self.value, "value"))
        object.__setattr__(
            self,
            "evidence_text",
            require_text(self.evidence_text, "evidence_text"),
        )
        object.__setattr__(self, "reason", require_text(self.reason, "reason"))
        object.__setattr__(
            self,
            "occurred_at",
            require_utc(self.occurred_at, "occurred_at"),
        )
        _validate_expected_sequence(self.expected_sequence)
        _normalize_run_at(self)


@dataclass(frozen=True, slots=True)
class ExpectedUtility:
    memory: MemoryReference
    mean: float

    def __post_init__(self) -> None:
        if not 0.0 <= self.mean <= 1.0:
            raise DomainError("expected utility mean must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class RecallReplayCase:
    case_id: str
    command: RecallMemory
    relevant: tuple[MemoryReference, ...] = ()
    forbidden: tuple[MemoryReference, ...] = ()
    expect_abstention: bool = False
    expected_utilities: tuple[ExpectedUtility, ...] = ()
    expected_error: ExpectedRecallRejection | None = None
    run_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", require_text(self.case_id, "case_id"))
        object.__setattr__(self, "relevant", tuple(self.relevant))
        object.__setattr__(self, "forbidden", tuple(self.forbidden))
        object.__setattr__(self, "expected_utilities", tuple(self.expected_utilities))
        _normalize_run_at(self)
        if len(set(self.relevant)) != len(self.relevant):
            raise DomainError("relevant memory references must be unique")
        if len(set(self.forbidden)) != len(self.forbidden):
            raise DomainError("forbidden memory references must be unique")
        if set(self.relevant) & set(self.forbidden):
            raise DomainError("a memory reference cannot be relevant and forbidden")
        utility_memories = tuple(expectation.memory for expectation in self.expected_utilities)
        if len(set(utility_memories)) != len(utility_memories):
            raise DomainError("expected utility memory references must be unique")
        if self.expect_abstention and self.relevant:
            raise DomainError("an abstention case cannot declare relevant memories")
        if self.expected_error is not None:
            if not isinstance(self.expected_error, ExpectedRecallRejection):
                raise DomainError("expected_error must be an ExpectedRecallRejection")
            if self.relevant or self.forbidden or self.expect_abstention or self.expected_utilities:
                raise DomainError("an expected-error recall cannot declare result labels")
            if self.expected_error is ExpectedRecallRejection.FUTURE_KNOWN_AT:
                if self.command.known_at is None:
                    raise DomainError("future_known_at rejection requires known_at")
                if self.run_at is not None and self.command.known_at <= self.run_at:
                    raise DomainError("future_known_at rejection requires known_at after run_at")


@dataclass(frozen=True, slots=True)
class OutcomeReplayCase:
    case_id: str
    trace_case_id: str
    memory: MemoryReference
    scope: Scope
    kind: OutcomeKind
    idempotency_key: str
    occurred_at: datetime
    weight: float = 1.0
    note: str | None = None
    expected_utility_mean: float | None = None
    expected_idempotent_replay: bool | None = None
    run_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", require_text(self.case_id, "case_id"))
        object.__setattr__(
            self,
            "trace_case_id",
            require_text(self.trace_case_id, "trace_case_id"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            require_text(self.idempotency_key, "idempotency_key"),
        )
        object.__setattr__(
            self,
            "occurred_at",
            require_utc(self.occurred_at, "occurred_at"),
        )
        if not isinstance(self.kind, OutcomeKind):
            raise DomainError("outcome kind must be an OutcomeKind")
        if not 0.0 < self.weight <= 10.0:
            raise DomainError("outcome weight must be in (0, 10]")
        if self.note is not None:
            normalized = self.note.strip()
            object.__setattr__(self, "note", normalized or None)
        if self.expected_utility_mean is not None and not 0.0 <= self.expected_utility_mean <= 1.0:
            raise DomainError("expected outcome utility mean must be between 0 and 1")
        if self.expected_idempotent_replay is not None and not isinstance(
            self.expected_idempotent_replay,
            bool,
        ):
            raise DomainError("expected outcome idempotent_replay must be a boolean")
        _normalize_run_at(self)


type ReplayCase = WriteReplayCase | CorrectionReplayCase | RecallReplayCase | OutcomeReplayCase


@dataclass(frozen=True, slots=True)
class EvaluationDataset:
    name: str
    version: str
    cases: tuple[ReplayCase, ...]
    recall_k: int = 5
    schema_version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", require_text(self.name, "dataset name"))
        object.__setattr__(self, "version", require_text(self.version, "dataset version"))
        object.__setattr__(self, "cases", tuple(self.cases))
        if not self.cases:
            raise DomainError("evaluation dataset must contain at least one case")
        if self.recall_k < 1:
            raise DomainError("recall_k must be positive")
        if self.schema_version not in {1, 2}:
            raise DomainError("unsupported evaluation schema_version")

        case_ids = tuple(case.case_id for case in self.cases)
        if len(set(case_ids)) != len(case_ids):
            raise DomainError("evaluation case_id values must be unique")

        available_memories: set[str] = set()
        available_traces: set[str] = set()
        previous_run_at: datetime | None = None
        for case in self.cases:
            if self.schema_version == 1:
                if isinstance(case, OutcomeReplayCase) or case.run_at is not None:
                    raise DomainError("schema v1 cannot contain timeline or outcome cases")
            elif case.run_at is None:
                raise DomainError("schema v2 requires run_at on every case")
            elif previous_run_at is not None and case.run_at < previous_run_at:
                raise DomainError("schema v2 run_at values must be non-decreasing")
            if case.run_at is not None:
                previous_run_at = case.run_at

            if isinstance(case, CorrectionReplayCase):
                _validate_reference_order(case.target, available_memories, case.case_id)
            elif isinstance(case, RecallReplayCase):
                if not self.recall_k <= case.command.limit <= 100:
                    raise DomainError(
                        "recall command limit must be at least recall_k and at most 100"
                    )
                for reference in case.relevant + case.forbidden:
                    _validate_reference_order(reference, available_memories, case.case_id)
                for expectation in case.expected_utilities:
                    _validate_reference_order(
                        expectation.memory,
                        available_memories,
                        case.case_id,
                    )
                if case.expected_error is None:
                    available_traces.add(case.case_id)
            elif isinstance(case, OutcomeReplayCase):
                _validate_reference_order(case.memory, available_memories, case.case_id)
                if case.trace_case_id not in available_traces:
                    raise DomainError(
                        f"case {case.case_id} references unavailable recall case "
                        f"{case.trace_case_id}"
                    )
            if isinstance(case, (WriteReplayCase, CorrectionReplayCase)):
                available_memories.add(case.case_id)

    @property
    def snapshot_hash(self) -> str:
        payload: dict[str, object] = {
            "schema": f"evolvable-memory-evaluation/v{self.schema_version}",
            "name": self.name,
            "version": self.version,
            "recall_k": self.recall_k,
            "cases": [_case_payload(case) for case in self.cases],
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


class MemoryReplayPort(Protocol):
    def remember_preference(self, command: RememberPreference) -> PreferenceResult: ...

    def correct_preference(self, command: CorrectPreference) -> PreferenceResult: ...

    def recall(self, command: RecallMemory) -> RecallResult: ...

    def record_outcome(self, command: RecordOutcome) -> OutcomeResult: ...


class ReplayClockPort(Protocol):
    def set(self, value: datetime) -> None: ...


@dataclass(frozen=True, slots=True)
class ReplayCaseResult:
    case_id: str
    kind: ReplayCaseKind
    passed: bool
    error: str | None = None
    record_id: UUID | None = None
    revision_id: UUID | None = None
    trace_id: UUID | None = None
    retrieved_revision_ids: tuple[UUID, ...] = ()
    recall_at_k: float | None = None
    reciprocal_rank: float | None = None
    abstention_correct: bool | None = None
    forbidden_hits: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    write_case_count: int
    correction_case_count: int
    recall_case_count: int
    outcome_case_count: int
    execution_failure_count: int
    recall_at_k: float | None
    mrr_at_k: float | None
    update_accuracy: float | None
    abstention_accuracy: float | None
    forbidden_hit_count: int


@dataclass(frozen=True, slots=True)
class HardGateCheck:
    name: str
    passed: bool
    observed: int | float | None
    comparator: str
    threshold: int | float


@dataclass(frozen=True, slots=True)
class HardGateReport:
    checks: tuple[HardGateCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def violations(self) -> tuple[str, ...]:
        return tuple(check.name for check in self.checks if not check.passed)


@dataclass(frozen=True, slots=True)
class HardGatePolicy:
    max_forbidden_hits: int = 0
    max_execution_failures: int = 0
    min_recall_at_k: float | None = None
    min_mrr: float | None = None
    min_update_accuracy: float | None = None
    min_abstention_accuracy: float | None = None

    def __post_init__(self) -> None:
        if min(self.max_forbidden_hits, self.max_execution_failures) < 0:
            raise DomainError("hard-gate maximum values must be non-negative")
        for name, value in (
            ("min_recall_at_k", self.min_recall_at_k),
            ("min_mrr", self.min_mrr),
            ("min_update_accuracy", self.min_update_accuracy),
            ("min_abstention_accuracy", self.min_abstention_accuracy),
        ):
            if value is not None and not 0.0 <= value <= 1.0:
                raise DomainError(f"{name} must be between 0 and 1")

    def evaluate(self, metrics: EvaluationMetrics) -> HardGateReport:
        checks = [
            HardGateCheck(
                name="forbidden_hits",
                passed=metrics.forbidden_hit_count <= self.max_forbidden_hits,
                observed=metrics.forbidden_hit_count,
                comparator="<=",
                threshold=self.max_forbidden_hits,
            ),
            HardGateCheck(
                name="execution_failures",
                passed=metrics.execution_failure_count <= self.max_execution_failures,
                observed=metrics.execution_failure_count,
                comparator="<=",
                threshold=self.max_execution_failures,
            ),
        ]
        _append_minimum_check(checks, "recall_at_k", metrics.recall_at_k, self.min_recall_at_k)
        _append_minimum_check(checks, "mrr_at_k", metrics.mrr_at_k, self.min_mrr)
        _append_minimum_check(
            checks,
            "update_accuracy",
            metrics.update_accuracy,
            self.min_update_accuracy,
        )
        _append_minimum_check(
            checks,
            "abstention_accuracy",
            metrics.abstention_accuracy,
            self.min_abstention_accuracy,
        )
        return HardGateReport(tuple(checks))


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    dataset_schema_version: int
    dataset_name: str
    dataset_version: str
    dataset_snapshot_hash: str
    recall_k: int
    cases: tuple[ReplayCaseResult, ...]
    metrics: EvaluationMetrics
    gates: HardGateReport


@dataclass(frozen=True, slots=True)
class _ResolvedMemory:
    record_id: UUID
    revision_id: UUID


class DeterministicReplayEvaluator:
    """Runs an ordered dataset without mutating evaluation policy or labels."""

    def __init__(
        self,
        application: MemoryReplayPort,
        *,
        clock: ReplayClockPort | None = None,
    ) -> None:
        self._application = application
        self._clock = clock

    def evaluate(
        self,
        dataset: EvaluationDataset,
        gate_policy: HardGatePolicy | None = None,
    ) -> EvaluationReport:
        if dataset.schema_version == 2 and self._clock is None:
            raise DomainError("schema v2 replay requires a controllable clock")
        resolved: dict[str, _ResolvedMemory] = {}
        resolved_traces: dict[str, UUID] = {}
        results: list[ReplayCaseResult] = []

        for case in dataset.cases:
            if case.run_at is not None:
                if self._clock is None:
                    raise DomainError("timeline replay requires a controllable clock")
                self._clock.set(case.run_at)
            if isinstance(case, WriteReplayCase):
                result = self._run_write(case)
            elif isinstance(case, CorrectionReplayCase):
                result = self._run_correction(case, resolved)
            elif isinstance(case, RecallReplayCase):
                result = self._run_recall(case, resolved, dataset.recall_k)
            else:
                result = self._run_outcome(case, resolved, resolved_traces)
            results.append(result)
            if (
                isinstance(case, (WriteReplayCase, CorrectionReplayCase))
                and result.record_id is not None
                and result.revision_id is not None
            ):
                resolved[case.case_id] = _ResolvedMemory(
                    record_id=result.record_id,
                    revision_id=result.revision_id,
                )
            if isinstance(case, RecallReplayCase) and result.trace_id is not None:
                resolved_traces[case.case_id] = result.trace_id

        metrics = _aggregate_metrics(dataset.cases, results)
        policy = gate_policy or HardGatePolicy()
        return EvaluationReport(
            dataset_schema_version=dataset.schema_version,
            dataset_name=dataset.name,
            dataset_version=dataset.version,
            dataset_snapshot_hash=dataset.snapshot_hash,
            recall_k=dataset.recall_k,
            cases=tuple(results),
            metrics=metrics,
            gates=policy.evaluate(metrics),
        )

    def _run_write(self, case: WriteReplayCase) -> ReplayCaseResult:
        try:
            result = self._application.remember_preference(case.command)
            error = _preference_expectation_error(
                result,
                expected_sequence=case.expected_sequence,
                expected_idempotent_replay=case.expected_idempotent_replay,
            )
            return ReplayCaseResult(
                case_id=case.case_id,
                kind=ReplayCaseKind.WRITE,
                passed=error is None,
                error=error,
                record_id=result.record_id,
                revision_id=result.revision_id,
            )
        except Exception as error:
            return _failed_case(case.case_id, ReplayCaseKind.WRITE, error)

    def _run_correction(
        self,
        case: CorrectionReplayCase,
        resolved: dict[str, _ResolvedMemory],
    ) -> ReplayCaseResult:
        try:
            target = _resolve_reference(case.target, resolved)
            command = CorrectPreference(
                scope=case.scope,
                record_id=target.record_id,
                source=case.source,
                idempotency_key=case.idempotency_key,
                value=case.value,
                evidence_text=case.evidence_text,
                reason=case.reason,
                occurred_at=case.occurred_at,
                expected_revision_id=(
                    target.revision_id if case.enforce_expected_revision else None
                ),
            )
            result = self._application.correct_preference(command)
            error = _preference_expectation_error(
                result,
                expected_sequence=case.expected_sequence,
                expected_idempotent_replay=case.expected_idempotent_replay,
            )
            return ReplayCaseResult(
                case_id=case.case_id,
                kind=ReplayCaseKind.CORRECTION,
                passed=error is None,
                error=error,
                record_id=result.record_id,
                revision_id=result.revision_id,
            )
        except Exception as error:
            return _failed_case(case.case_id, ReplayCaseKind.CORRECTION, error)

    def _run_recall(
        self,
        case: RecallReplayCase,
        resolved: dict[str, _ResolvedMemory],
        recall_k: int,
    ) -> ReplayCaseResult:
        try:
            relevant = tuple(_resolve_reference(reference, resolved) for reference in case.relevant)
            forbidden = {_resolve_reference(reference, resolved) for reference in case.forbidden}
            expected_utilities = {
                _resolve_reference(expectation.memory, resolved): expectation.mean
                for expectation in case.expected_utilities
            }
            trace = self._application.recall(case.command)
            if case.expected_error is not None:
                return ReplayCaseResult(
                    case_id=case.case_id,
                    kind=ReplayCaseKind.RECALL,
                    passed=False,
                    error=f"expected {case.expected_error.value}",
                    trace_id=trace.id,
                )
            ranked = tuple(
                _ResolvedMemory(item.record_id, item.revision_id) for item in trace.items
            )
            top_k = ranked[:recall_k]
            recall_score: float | None = None
            reciprocal_rank: float | None = None
            if relevant:
                relevant_set = set(relevant)
                recall_score = len(relevant_set & set(top_k)) / len(relevant_set)
                reciprocal_rank = next(
                    (
                        1.0 / rank
                        for rank, memory in enumerate(top_k, start=1)
                        if memory in relevant_set
                    ),
                    0.0,
                )
            abstention_correct = not ranked if case.expect_abstention else None
            forbidden_hits = tuple(memory.revision_id for memory in ranked if memory in forbidden)
            utility_error = _utility_expectation_error(trace, expected_utilities)
            return ReplayCaseResult(
                case_id=case.case_id,
                kind=ReplayCaseKind.RECALL,
                passed=utility_error is None,
                error=utility_error,
                trace_id=trace.id,
                retrieved_revision_ids=tuple(memory.revision_id for memory in ranked),
                recall_at_k=recall_score,
                reciprocal_rank=reciprocal_rank,
                abstention_correct=abstention_correct,
                forbidden_hits=forbidden_hits,
            )
        except Exception as error:
            if _matches_expected_recall_rejection(case.expected_error, error):
                return ReplayCaseResult(
                    case_id=case.case_id,
                    kind=ReplayCaseKind.RECALL,
                    passed=True,
                )
            return ReplayCaseResult(
                case_id=case.case_id,
                kind=ReplayCaseKind.RECALL,
                passed=False,
                error=type(error).__name__,
                recall_at_k=0.0 if case.relevant else None,
                reciprocal_rank=0.0 if case.relevant else None,
                abstention_correct=False if case.expect_abstention else None,
            )

    def _run_outcome(
        self,
        case: OutcomeReplayCase,
        resolved: dict[str, _ResolvedMemory],
        resolved_traces: dict[str, UUID],
    ) -> ReplayCaseResult:
        try:
            memory = _resolve_reference(case.memory, resolved)
            trace_id = resolved_traces.get(case.trace_case_id)
            if trace_id is None:
                raise DomainError(f"recall case {case.trace_case_id} did not produce a trace")
            result = self._application.record_outcome(
                RecordOutcome(
                    scope=case.scope,
                    trace_id=trace_id,
                    revision_id=memory.revision_id,
                    kind=case.kind,
                    idempotency_key=case.idempotency_key,
                    occurred_at=case.occurred_at,
                    weight=case.weight,
                    note=case.note,
                )
            )
            error = _outcome_expectation_error(case, result)
            return ReplayCaseResult(
                case_id=case.case_id,
                kind=ReplayCaseKind.OUTCOME,
                passed=error is None,
                error=error,
                record_id=memory.record_id,
                revision_id=memory.revision_id,
                trace_id=trace_id,
            )
        except Exception as error:
            return _failed_case(case.case_id, ReplayCaseKind.OUTCOME, error)


def _validate_expected_sequence(expected: int | None) -> None:
    if expected is not None and expected < 1:
        raise DomainError("expected_sequence must be positive")


def _normalize_run_at(case: ReplayCase) -> None:
    run_at = case.run_at
    if run_at is not None:
        object.__setattr__(case, "run_at", require_utc(run_at, "run_at"))


def _validate_reference_order(
    reference: MemoryReference,
    available: set[str],
    owner_case_id: str,
) -> None:
    if reference.case_id is not None and reference.case_id not in available:
        raise DomainError(
            f"case {owner_case_id} references unavailable memory case {reference.case_id}"
        )


def _resolve_reference(
    reference: MemoryReference,
    resolved: dict[str, _ResolvedMemory],
) -> _ResolvedMemory:
    if reference.case_id is not None:
        result = resolved.get(reference.case_id)
        if result is None:
            raise DomainError(f"memory case {reference.case_id} did not produce a revision")
        return result
    if reference.record_id is None or reference.revision_id is None:
        raise DomainError("direct memory reference is incomplete")
    return _ResolvedMemory(reference.record_id, reference.revision_id)


def _preference_expectation_error(
    result: PreferenceResult,
    *,
    expected_sequence: int | None,
    expected_idempotent_replay: bool | None,
) -> str | None:
    if expected_sequence is not None and result.sequence != expected_sequence:
        return f"expected sequence {expected_sequence}, received {result.sequence}"
    if (
        expected_idempotent_replay is not None
        and result.idempotent_replay is not expected_idempotent_replay
    ):
        return (
            "expected idempotent_replay "
            f"{expected_idempotent_replay}, received {result.idempotent_replay}"
        )
    return None


def _utility_expectation_error(
    trace: RecallResult,
    expected: dict[_ResolvedMemory, float],
) -> str | None:
    actual = {
        _ResolvedMemory(item.record_id, item.revision_id): item.breakdown.utility
        for item in trace.items
    }
    for memory, expected_mean in expected.items():
        actual_mean = actual.get(memory)
        if actual_mean is None:
            return "expected utility memory was not recalled"
        if not isclose(actual_mean, expected_mean, rel_tol=0.0, abs_tol=1e-9):
            return "recalled utility did not match expectation"
    return None


def _matches_expected_recall_rejection(
    expected: ExpectedRecallRejection | None,
    error: Exception,
) -> bool:
    if expected is ExpectedRecallRejection.FUTURE_KNOWN_AT:
        return type(error) is DomainError and str(error) == "known_at must not be in the future"
    return False


def _outcome_expectation_error(
    case: OutcomeReplayCase,
    result: OutcomeResult,
) -> str | None:
    if (
        case.expected_idempotent_replay is not None
        and result.idempotent_replay is not case.expected_idempotent_replay
    ):
        return (
            "expected idempotent_replay "
            f"{case.expected_idempotent_replay}, received {result.idempotent_replay}"
        )
    if case.expected_utility_mean is not None and not isclose(
        result.utility.mean,
        case.expected_utility_mean,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        return "outcome utility did not match expectation"
    return None


def _failed_case(
    case_id: str,
    kind: ReplayCaseKind,
    error: Exception,
) -> ReplayCaseResult:
    return ReplayCaseResult(
        case_id=case_id,
        kind=kind,
        passed=False,
        error=type(error).__name__,
    )


def _aggregate_metrics(
    cases: tuple[ReplayCase, ...],
    results: list[ReplayCaseResult],
) -> EvaluationMetrics:
    recall_scores: list[float] = []
    reciprocal_ranks: list[float] = []
    abstentions: list[float] = []
    update_results: list[float] = []
    for result in results:
        if result.recall_at_k is not None:
            recall_scores.append(result.recall_at_k)
        if result.reciprocal_rank is not None:
            reciprocal_ranks.append(result.reciprocal_rank)
        if result.abstention_correct is not None:
            abstentions.append(1.0 if result.abstention_correct else 0.0)
    for case, result in zip(cases, results, strict=True):
        if isinstance(case, CorrectionReplayCase) and (
            case.expected_sequence is not None or case.expected_idempotent_replay is not None
        ):
            update_results.append(1.0 if result.passed else 0.0)
    return EvaluationMetrics(
        write_case_count=sum(isinstance(case, WriteReplayCase) for case in cases),
        correction_case_count=sum(isinstance(case, CorrectionReplayCase) for case in cases),
        recall_case_count=sum(isinstance(case, RecallReplayCase) for case in cases),
        outcome_case_count=sum(isinstance(case, OutcomeReplayCase) for case in cases),
        execution_failure_count=sum(not result.passed for result in results),
        recall_at_k=_mean(recall_scores),
        mrr_at_k=_mean(reciprocal_ranks),
        update_accuracy=_mean(update_results),
        abstention_accuracy=_mean(abstentions),
        forbidden_hit_count=sum(len(result.forbidden_hits) for result in results),
    )


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _append_minimum_check(
    checks: list[HardGateCheck],
    name: str,
    observed: float | None,
    threshold: float | None,
) -> None:
    if threshold is None:
        return
    checks.append(
        HardGateCheck(
            name=name,
            passed=observed is not None and observed >= threshold,
            observed=observed,
            comparator=">=",
            threshold=threshold,
        )
    )


def _reference_payload(reference: MemoryReference) -> dict[str, object]:
    if reference.case_id is not None:
        return {"case_id": reference.case_id}
    return {
        "record_id": str(reference.record_id),
        "revision_id": str(reference.revision_id),
    }


def _scope_payload(scope: Scope) -> dict[str, object]:
    return {
        "tenant_id": scope.tenant_id,
        "subject_id": scope.subject_id,
    }


def _context_payload(context: ContextSignature) -> list[list[str]]:
    return [[key, value] for key, value in context.facets]


def _remember_payload(command: RememberPreference) -> dict[str, object]:
    return {
        "scope": _scope_payload(command.scope),
        "source": command.source,
        "idempotency_key": command.idempotency_key,
        "key": command.key,
        "value": command.value,
        "context": _context_payload(command.context),
        "evidence_text": command.evidence_text,
        "confidence": command.confidence,
        "occurred_at": command.occurred_at.isoformat(),
    }


def _with_run_at(payload: dict[str, object], case: ReplayCase) -> dict[str, object]:
    if case.run_at is not None:
        payload["run_at"] = case.run_at.isoformat()
    return payload


def _recall_payload(command: RecallMemory) -> dict[str, object]:
    return {
        "scope": _scope_payload(command.scope),
        "query": command.query,
        "context": _context_payload(command.context),
        "limit": command.limit,
        "valid_at": command.valid_at.isoformat() if command.valid_at is not None else None,
        "known_at": command.known_at.isoformat() if command.known_at is not None else None,
    }


def _case_payload(case: ReplayCase) -> dict[str, object]:
    if isinstance(case, WriteReplayCase):
        return _with_run_at(
            {
                "type": ReplayCaseKind.WRITE.value,
                "case_id": case.case_id,
                "command": _remember_payload(case.command),
                "expected_sequence": case.expected_sequence,
                "expected_idempotent_replay": case.expected_idempotent_replay,
            },
            case,
        )
    if isinstance(case, CorrectionReplayCase):
        return _with_run_at(
            {
                "type": ReplayCaseKind.CORRECTION.value,
                "case_id": case.case_id,
                "target": _reference_payload(case.target),
                "scope": _scope_payload(case.scope),
                "source": case.source,
                "idempotency_key": case.idempotency_key,
                "value": case.value,
                "evidence_text": case.evidence_text,
                "reason": case.reason,
                "occurred_at": case.occurred_at.isoformat(),
                "enforce_expected_revision": case.enforce_expected_revision,
                "expected_sequence": case.expected_sequence,
                "expected_idempotent_replay": case.expected_idempotent_replay,
            },
            case,
        )
    if isinstance(case, RecallReplayCase):
        payload: dict[str, object] = {
            "type": ReplayCaseKind.RECALL.value,
            "case_id": case.case_id,
            "command": _recall_payload(case.command),
            "relevant": [_reference_payload(reference) for reference in case.relevant],
            "forbidden": [_reference_payload(reference) for reference in case.forbidden],
            "expect_abstention": case.expect_abstention,
        }
        if case.expected_utilities:
            payload["expected_utilities"] = [
                {
                    "memory": _reference_payload(expectation.memory),
                    "mean": expectation.mean,
                }
                for expectation in case.expected_utilities
            ]
        if case.expected_error is not None:
            payload["expected_error"] = case.expected_error.value
        return _with_run_at(payload, case)
    return {
        "type": ReplayCaseKind.OUTCOME.value,
        "case_id": case.case_id,
        "trace_case_id": case.trace_case_id,
        "memory": _reference_payload(case.memory),
        "scope": _scope_payload(case.scope),
        "kind": case.kind.value,
        "idempotency_key": case.idempotency_key,
        "occurred_at": case.occurred_at.isoformat(),
        "weight": case.weight,
        "note": case.note,
        "expected_utility_mean": case.expected_utility_mean,
        "expected_idempotent_replay": case.expected_idempotent_replay,
        "run_at": case.run_at.isoformat() if case.run_at is not None else None,
    }
