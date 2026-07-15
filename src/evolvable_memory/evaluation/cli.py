from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from evolvable_memory.adapters.in_memory import InMemoryMemoryStore
from evolvable_memory.application.evaluation import (
    CorrectionReplayCase,
    DeterministicReplayEvaluator,
    EvaluationDataset,
    EvaluationReport,
    HardGatePolicy,
    WriteReplayCase,
)
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.domain.common import DomainError
from evolvable_memory.evaluation.loader import (
    DatasetValidationError,
    list_builtin_datasets,
    load_dataset,
)

_DEFAULT_DATASET = "builtin:smoke-v1"


@dataclass(frozen=True, slots=True)
class _EvaluationClock:
    current: datetime

    def now(self) -> datetime:
        return self.current


class _SequentialIds:
    def __init__(self) -> None:
        self._value = 0

    def new(self) -> UUID:
        self._value += 1
        return UUID(int=self._value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evolvable-memory-eval",
        description="Run deterministic retrieval and memory-invariant quality gates.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list packaged synthetic datasets")

    validate = commands.add_parser("validate", help="validate a dataset without replaying it")
    _dataset_argument(validate)

    run_parser = commands.add_parser("run", help="replay a dataset and enforce hard gates")
    _dataset_argument(run_parser)
    run_parser.add_argument("--format", choices=("text", "json"), default="text")
    run_parser.add_argument("--report", type=Path, help="also write a redacted JSON report")
    run_parser.add_argument("--min-recall-at-k", type=_unit_interval, default=1.0)
    run_parser.add_argument("--min-mrr", type=_unit_interval, default=1.0)
    run_parser.add_argument("--min-update-accuracy", type=_unit_interval, default=1.0)
    run_parser.add_argument("--min-abstention-accuracy", type=_unit_interval, default=1.0)
    run_parser.add_argument("--max-forbidden-hits", type=_non_negative_integer, default=0)
    run_parser.add_argument("--max-execution-failures", type=_non_negative_integer, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "list":
        for dataset_name in list_builtin_datasets():
            print(dataset_name)
        return 0
    try:
        dataset = load_dataset(args.dataset)
    except DatasetValidationError as error:
        print(f"dataset error: {error}", file=sys.stderr)
        return 2
    if args.command == "validate":
        print(
            f"valid dataset {dataset.name}@{dataset.version} "
            f"({len(dataset.cases)} cases, sha256:{dataset.snapshot_hash})"
        )
        return 0
    return _run_evaluation(args, dataset)


def _run_evaluation(args: argparse.Namespace, dataset: EvaluationDataset) -> int:
    policy = HardGatePolicy(
        max_forbidden_hits=args.max_forbidden_hits,
        max_execution_failures=args.max_execution_failures,
        min_recall_at_k=args.min_recall_at_k,
        min_mrr=args.min_mrr,
        min_update_accuracy=args.min_update_accuracy,
        min_abstention_accuracy=args.min_abstention_accuracy,
    )
    store = InMemoryMemoryStore()
    application = MemoryApplication(
        store=store,
        clock=_EvaluationClock(_evaluation_time(dataset)),
        ids=_SequentialIds(),
    )
    try:
        report = DeterministicReplayEvaluator(application).evaluate(dataset, policy)
    except Exception as error:
        print(f"evaluation error: {type(error).__name__}", file=sys.stderr)
        return 3
    finally:
        application.close()

    serialized = _report_payload(report)
    json_report = json.dumps(serialized, ensure_ascii=False, indent=2, sort_keys=True)
    if args.report is not None:
        try:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(f"{json_report}\n", encoding="utf-8")
        except OSError:
            print("evaluation error: report could not be written", file=sys.stderr)
            return 3
    if args.format == "json":
        print(json_report)
    else:
        print(_text_report(report))
    return 0 if report.gates.passed else 1


def _evaluation_time(dataset: EvaluationDataset) -> datetime:
    timestamps = [
        case.command.occurred_at for case in dataset.cases if isinstance(case, WriteReplayCase)
    ]
    timestamps.extend(
        case.occurred_at for case in dataset.cases if isinstance(case, CorrectionReplayCase)
    )
    return max(timestamps, default=datetime(2026, 1, 1, tzinfo=UTC))


def _report_payload(report: EvaluationReport) -> dict[str, object]:
    return {
        "schema_version": 1,
        "dataset": {
            "name": report.dataset_name,
            "version": report.dataset_version,
            "snapshot_sha256": report.dataset_snapshot_hash,
            "recall_k": report.recall_k,
        },
        "status": "passed" if report.gates.passed else "failed",
        "metrics": {
            "write_case_count": report.metrics.write_case_count,
            "correction_case_count": report.metrics.correction_case_count,
            "recall_case_count": report.metrics.recall_case_count,
            "execution_failure_count": report.metrics.execution_failure_count,
            "recall_at_k": report.metrics.recall_at_k,
            "mrr_at_k": report.metrics.mrr_at_k,
            "update_accuracy": report.metrics.update_accuracy,
            "abstention_accuracy": report.metrics.abstention_accuracy,
            "forbidden_hit_count": report.metrics.forbidden_hit_count,
        },
        "gates": [
            {
                "name": check.name,
                "passed": check.passed,
                "observed": check.observed,
                "comparator": check.comparator,
                "threshold": check.threshold,
            }
            for check in report.gates.checks
        ],
        "cases": [
            {
                "id": case.case_id,
                "kind": case.kind.value,
                "execution_passed": case.passed,
                "error": case.error,
                "retrieved_count": len(case.retrieved_revision_ids),
                "recall_at_k": case.recall_at_k,
                "reciprocal_rank": case.reciprocal_rank,
                "abstention_correct": case.abstention_correct,
                "forbidden_hit_count": len(case.forbidden_hits),
            }
            for case in report.cases
        ],
    }


def _text_report(report: EvaluationReport) -> str:
    metrics = report.metrics
    status = "PASS" if report.gates.passed else "FAIL"
    metric_lines = (
        f"Recall@{report.recall_k}: {_metric(metrics.recall_at_k)}",
        f"MRR@{report.recall_k}: {_metric(metrics.mrr_at_k)}",
        f"Update accuracy: {_metric(metrics.update_accuracy)}",
        f"Abstention accuracy: {_metric(metrics.abstention_accuracy)}",
        f"Forbidden hits: {metrics.forbidden_hit_count}",
        f"Execution failures: {metrics.execution_failure_count}",
    )
    failed_gates = ", ".join(report.gates.violations) or "none"
    return "\n".join(
        (
            f"Memory evaluation: {status}",
            f"Dataset: {report.dataset_name}@{report.dataset_version}",
            f"Snapshot: sha256:{report.dataset_snapshot_hash}",
            *metric_lines,
            f"Failed gates: {failed_gates}",
        )
    )


def _metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _dataset_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset", default=_DEFAULT_DATASET)


def _unit_interval(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a number between 0 and 1") from error
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return value


def _non_negative_integer(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from error
    if value < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return value


def run() -> None:
    try:
        raise SystemExit(main())
    except DomainError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    run()
