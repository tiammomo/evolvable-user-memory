from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from evolvable_memory.evaluation.cli import main
from evolvable_memory.evaluation.loader import (
    DatasetValidationError,
    dataset_from_json,
    list_builtin_datasets,
    load_dataset,
)


def _minimal_dataset(*, query: str = "vegetarian diet") -> dict[str, object]:
    scope = {"tenant_id": "eval", "subject_id": "alice"}
    return {
        "schema_version": 1,
        "name": "test-dataset",
        "version": "1.0.0",
        "recall_k": 1,
        "cases": [
            {
                "type": "write",
                "id": "write-1",
                "scope": scope,
                "source": "synthetic-test",
                "idempotency_key": "write/1",
                "key": "diet.preference",
                "value": "vegetarian",
                "context": {},
                "evidence_text": "I follow a vegetarian diet",
                "confidence": 0.9,
                "occurred_at": "2026-01-01T00:00:00Z",
                "expected": {"sequence": 1, "idempotent_replay": False},
            },
            {
                "type": "recall",
                "id": "recall-1",
                "scope": scope,
                "query": query,
                "context": {},
                "limit": 1,
                "expected": {
                    "relevant": [{"case_id": "write-1"}],
                    "forbidden": [],
                    "abstain": False,
                },
            },
        ],
    }


def _minimal_timeline_dataset() -> dict[str, object]:
    dataset = _minimal_dataset()
    dataset["schema_version"] = 2
    cases = dataset["cases"]
    cases[0]["run_at"] = "2026-01-01T00:00:00Z"  # type: ignore[index]
    cases[1]["run_at"] = "2026-01-02T00:00:00Z"  # type: ignore[index]
    return dataset


def _write_dataset(path: Path, dataset: dict[str, object]) -> None:
    path.write_text(json.dumps(dataset), encoding="utf-8")


def test_builtin_dataset_is_versioned_deterministic_and_packaged() -> None:
    first = load_dataset("builtin:smoke-v1")
    second = load_dataset("builtin:smoke-v1")

    assert list_builtin_datasets() == ("builtin:smoke-v1", "builtin:temporal-v1")
    assert first.name == "builtin-smoke-v1"
    assert first.schema_version == 1
    assert len(first.cases) == 17
    assert first.snapshot_hash == second.snapshot_hash
    assert first.snapshot_hash == "8b95b279752a66bfc63a85cccce1fd774170030e9ae64ee9d7009434cec97bf4"


def test_temporal_dataset_has_a_versioned_timeline_and_outcome_replay() -> None:
    dataset = load_dataset("builtin:temporal-v1")

    assert dataset.name == "builtin-temporal-v1"
    assert dataset.schema_version == 2
    assert len(dataset.cases) == 16
    assert all(case.run_at is not None for case in dataset.cases)
    assert tuple(case.run_at for case in dataset.cases) == tuple(
        sorted(case.run_at for case in dataset.cases if case.run_at is not None)
    )


def test_external_dataset_loads_with_the_same_strict_contract(tmp_path: Path) -> None:
    dataset_path = tmp_path / "evaluation.json"
    _write_dataset(dataset_path, _minimal_dataset())

    loaded = load_dataset(str(dataset_path))

    assert loaded.name == "test-dataset"
    assert loaded.recall_k == 1
    assert len(loaded.cases) == 2


def test_temporal_recall_fields_are_validated_and_change_the_snapshot() -> None:
    current = _minimal_dataset()
    temporal = deepcopy(current)
    temporal_recall = temporal["cases"][1]  # type: ignore[index]
    temporal_recall["valid_at"] = "2026-01-02T00:00:00+08:00"  # type: ignore[index]
    temporal_recall["known_at"] = "2026-01-02T00:00:00Z"  # type: ignore[index]

    current_dataset = dataset_from_json(json.dumps(current))
    temporal_dataset = dataset_from_json(json.dumps(temporal))

    assert temporal_dataset.snapshot_hash != current_dataset.snapshot_hash

    temporal_recall["known_at"] = "2026-01-02T00:00:00"  # type: ignore[index]
    with pytest.raises(DatasetValidationError, match="UTC offset"):
        dataset_from_json(json.dumps(temporal))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data["cases"][0].pop("run_at"), "missing required"),  # type: ignore[index]
        (
            lambda data: data["cases"][1].update(run_at="2025-12-31T00:00:00Z"),  # type: ignore[index]
            "non-decreasing",
        ),
        (
            lambda data: data["cases"][1]["expected"].update(  # type: ignore[index]
                relevant=[], forbidden=[], abstain=False, error="RuntimeError"
            ),
            "unsupported recall rejection",
        ),
        (
            lambda data: data["cases"][1]["expected"].update(  # type: ignore[index]
                relevant=[], forbidden=[], abstain=False, error="future_known_at"
            ),
            "requires known_at",
        ),
        (
            lambda data: data["cases"][1]["expected"].update(  # type: ignore[index]
                utilities=[{"memory": {"case_id": "write-1"}, "mean": 2.0}]
            ),
            "utility mean",
        ),
    ],
)
def test_timeline_schema_fails_closed_on_ambiguous_time_or_expectations(
    mutation: object,
    message: str,
) -> None:
    dataset = _minimal_timeline_dataset()
    assert callable(mutation)
    mutation(dataset)

    with pytest.raises(DatasetValidationError, match=message):
        dataset_from_json(json.dumps(dataset))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda data: data.update(schema_version=3), "unsupported"),
        (lambda data: data.update(unexpected=True), "unknown fields"),
        (lambda data: data.pop("name"), "missing required"),
        (lambda data: data["cases"][0].update(type="erase"), "unsupported"),  # type: ignore[index,union-attr]
        (lambda data: data["cases"][0].update(unexpected=True), "unknown fields"),  # type: ignore[index,union-attr]
        (lambda data: data["cases"][0].update(occurred_at="2026-01-01"), "UTC offset"),  # type: ignore[index,union-attr]
        (lambda data: data["cases"][1].update(limit=0), "at least"),  # type: ignore[index,union-attr]
        (
            lambda data: data["cases"][1]["expected"].update(  # type: ignore[index,union-attr]
                relevant=[], forbidden=[], abstain=False
            ),
            "evaluation label",
        ),
    ],
)
def test_dataset_schema_rejects_ambiguous_or_unversioned_input(
    mutation: object,
    message: str,
) -> None:
    dataset = deepcopy(_minimal_dataset())
    assert callable(mutation)
    mutation(dataset)

    with pytest.raises(DatasetValidationError, match=message):
        dataset_from_json(json.dumps(dataset))


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("not-json", "not valid JSON"),
        ("[]", "must be an object"),
        ('{"schema_version": 1, "schema_version": 1}', "duplicate field"),
        ('{"confidence": NaN}', "non-finite number"),
    ],
)
def test_dataset_json_boundary_fails_closed(source: str, message: str) -> None:
    with pytest.raises(DatasetValidationError, match=message):
        dataset_from_json(source)


def test_reference_must_point_to_an_earlier_memory_case() -> None:
    dataset = _minimal_dataset()
    recall = dataset["cases"][1]  # type: ignore[index]
    recall["expected"]["relevant"] = [{"case_id": "future-write"}]  # type: ignore[index]

    with pytest.raises(DatasetValidationError, match="unavailable memory case"):
        dataset_from_json(json.dumps(dataset))


def test_cli_lists_and_validates_the_builtin_dataset(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["list"]) == 0
    assert capsys.readouterr().out.strip().splitlines() == [
        "builtin:smoke-v1",
        "builtin:temporal-v1",
    ]

    assert main(["validate", "--dataset", "builtin:smoke-v1"]) == 0
    output = capsys.readouterr().out
    assert "valid dataset builtin-smoke-v1@1.0.0" in output
    assert "sha256:" in output

    assert main(["validate", "--dataset", "builtin:temporal-v1"]) == 0
    output = capsys.readouterr().out
    assert "valid dataset builtin-temporal-v1@1.0.0 (16 cases" in output


def test_cli_runs_temporal_outcome_and_expected_rejection_gates(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["run", "--dataset", "builtin:temporal-v1", "--format", "json"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "passed"
    assert report["metrics"] == {
        "abstention_accuracy": 1.0,
        "correction_case_count": 2,
        "execution_failure_count": 0,
        "forbidden_hit_count": 0,
        "mrr_at_k": 1.0,
        "outcome_case_count": 2,
        "recall_at_k": 1.0,
        "recall_case_count": 11,
        "update_accuracy": 1.0,
        "write_case_count": 1,
    }
    future_known = next(
        case for case in report["cases"] if case["id"] == "future-known-at-is-rejected"
    )
    assert future_known["execution_passed"] is True
    assert future_known["error"] is None


def test_cli_run_is_deterministic_redacted_and_ignores_runtime_database_env(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMF_STORE", "postgres")
    monkeypatch.setenv("EMF_DATABASE_URL", "postgresql://must-not-be-used")
    first_report = tmp_path / "first.json"
    second_report = tmp_path / "second.json"

    assert (
        main(
            [
                "run",
                "--dataset",
                "builtin:smoke-v1",
                "--format",
                "json",
                "--report",
                str(first_report),
            ]
        )
        == 0
    )
    stdout_report = json.loads(capsys.readouterr().out)
    assert main(["run", "--report", str(second_report)]) == 0
    text_output = capsys.readouterr().out

    first = first_report.read_text(encoding="utf-8")
    second = second_report.read_text(encoding="utf-8")
    assert first == second
    assert stdout_report["status"] == "passed"
    assert stdout_report["metrics"]["forbidden_hit_count"] == 0
    assert stdout_report["metrics"]["abstention_accuracy"] == 1.0
    assert "Memory evaluation: PASS" in text_output
    assert "evidence_text" not in first
    assert "I follow a vegetarian diet" not in first
    assert "vegetarian" not in first
    assert "Kyoto" not in first


def test_cli_returns_quality_data_and_delivery_exit_codes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failing_path = tmp_path / "failing.json"
    _write_dataset(failing_path, _minimal_dataset(query="preferred code editor theme"))

    assert main(["run", "--dataset", str(failing_path)]) == 1
    assert "Memory evaluation: FAIL" in capsys.readouterr().out

    assert main(["validate", "--dataset", "builtin:missing"]) == 2
    assert "dataset error" in capsys.readouterr().err

    assert main(["run", "--report", str(tmp_path)]) == 3
    assert "report could not be written" in capsys.readouterr().err


@pytest.mark.parametrize(
    "arguments",
    [
        ["run", "--min-recall-at-k", "1.1"],
        ["run", "--max-forbidden-hits", "-1"],
    ],
)
def test_cli_rejects_invalid_gate_thresholds(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        main(arguments)

    assert raised.value.code == 2
