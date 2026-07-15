from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from importlib.resources import files
from math import isfinite
from pathlib import Path
from typing import NoReturn, cast
from uuid import UUID

from evolvable_memory.application.commands import RecallMemory, RememberPreference
from evolvable_memory.application.evaluation import (
    CorrectionReplayCase,
    EvaluationDataset,
    MemoryReference,
    RecallReplayCase,
    ReplayCase,
    WriteReplayCase,
)
from evolvable_memory.domain.common import ContextSignature, DomainError, Scope

_SCHEMA_VERSION = 1
_BUILTIN_DATASETS = {"smoke-v1": "smoke-v1.json"}


class DatasetValidationError(ValueError):
    """The external evaluation dataset does not satisfy the versioned contract."""


def list_builtin_datasets() -> tuple[str, ...]:
    return tuple(sorted(f"builtin:{name}" for name in _BUILTIN_DATASETS))


def load_dataset(specifier: str) -> EvaluationDataset:
    normalized = specifier.strip()
    if not normalized:
        raise DatasetValidationError("dataset specifier must not be blank")
    if normalized.startswith("builtin:"):
        name = normalized.removeprefix("builtin:")
        resource_name = _BUILTIN_DATASETS.get(name)
        if resource_name is None:
            raise DatasetValidationError("unknown built-in dataset")
        try:
            source = (
                files("evolvable_memory.evaluation")
                .joinpath("data", resource_name)
                .read_text(encoding="utf-8")
            )
        except (FileNotFoundError, OSError) as error:
            raise DatasetValidationError("built-in dataset resource is unavailable") from error
    else:
        try:
            source = Path(normalized).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError) as error:
            raise DatasetValidationError("dataset file cannot be read") from error
    return dataset_from_json(source)


def dataset_from_json(source: str) -> EvaluationDataset:
    try:
        raw: object = json.loads(
            source,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite,
        )
    except json.JSONDecodeError as error:
        raise DatasetValidationError("dataset is not valid JSON") from error
    except DatasetValidationError:
        raise
    try:
        return _parse_dataset(_object(raw, "dataset"))
    except DatasetValidationError:
        raise
    except DomainError as error:
        raise DatasetValidationError(f"dataset violates the evaluation contract: {error}") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DatasetValidationError("dataset JSON contains a duplicate field")
        result[key] = value
    return result


def _reject_non_finite(_: str) -> NoReturn:
    raise DatasetValidationError("dataset JSON contains a non-finite number")


def _parse_dataset(raw: dict[str, object]) -> EvaluationDataset:
    _fields(
        raw,
        path="dataset",
        required={"schema_version", "name", "version", "recall_k", "cases"},
        allowed={"schema_version", "name", "version", "recall_k", "cases"},
    )
    schema_version = _integer(raw["schema_version"], "dataset.schema_version")
    if schema_version != _SCHEMA_VERSION:
        raise DatasetValidationError("unsupported dataset schema_version")
    recall_k = _integer(raw["recall_k"], "dataset.recall_k")
    cases = tuple(
        _parse_case(case, index=index, recall_k=recall_k)
        for index, case in enumerate(_array(raw["cases"], "dataset.cases"))
    )
    return EvaluationDataset(
        name=_text(raw["name"], "dataset.name"),
        version=_text(raw["version"], "dataset.version"),
        recall_k=recall_k,
        cases=cases,
    )


def _parse_case(raw: object, *, index: int, recall_k: int) -> ReplayCase:
    value = _object(raw, f"dataset.cases[{index}]")
    case_type = _text(value.get("type"), f"dataset.cases[{index}].type")
    if case_type == "write":
        return _parse_write(value, index)
    if case_type == "correction":
        return _parse_correction(value, index)
    if case_type == "recall":
        return _parse_recall(value, index, recall_k)
    raise DatasetValidationError(f"dataset.cases[{index}].type is unsupported")


def _parse_write(raw: dict[str, object], index: int) -> WriteReplayCase:
    path = f"dataset.cases[{index}]"
    required = {
        "type",
        "id",
        "scope",
        "source",
        "idempotency_key",
        "key",
        "value",
        "context",
        "evidence_text",
        "confidence",
        "occurred_at",
    }
    _fields(raw, path=path, required=required, allowed=required | {"expected"})
    sequence, idempotent = _preference_expectation(raw.get("expected"), f"{path}.expected")
    return WriteReplayCase(
        case_id=_text(raw["id"], f"{path}.id"),
        command=RememberPreference(
            scope=_scope(raw["scope"], f"{path}.scope"),
            source=_text(raw["source"], f"{path}.source"),
            idempotency_key=_text(raw["idempotency_key"], f"{path}.idempotency_key"),
            key=_text(raw["key"], f"{path}.key"),
            value=_text(raw["value"], f"{path}.value"),
            context=_context(raw["context"], f"{path}.context"),
            evidence_text=_text(raw["evidence_text"], f"{path}.evidence_text"),
            confidence=_number(raw["confidence"], f"{path}.confidence"),
            occurred_at=_datetime(raw["occurred_at"], f"{path}.occurred_at"),
        ),
        expected_sequence=sequence,
        expected_idempotent_replay=idempotent,
    )


def _parse_correction(raw: dict[str, object], index: int) -> CorrectionReplayCase:
    path = f"dataset.cases[{index}]"
    required = {
        "type",
        "id",
        "target",
        "scope",
        "source",
        "idempotency_key",
        "value",
        "evidence_text",
        "reason",
        "occurred_at",
    }
    allowed = required | {"enforce_expected_revision", "expected"}
    _fields(raw, path=path, required=required, allowed=allowed)
    sequence, idempotent = _preference_expectation(raw.get("expected"), f"{path}.expected")
    enforce_expected_revision = raw.get("enforce_expected_revision", True)
    if not isinstance(enforce_expected_revision, bool):
        raise DatasetValidationError(f"{path}.enforce_expected_revision must be a boolean")
    return CorrectionReplayCase(
        case_id=_text(raw["id"], f"{path}.id"),
        target=_reference(raw["target"], f"{path}.target"),
        scope=_scope(raw["scope"], f"{path}.scope"),
        source=_text(raw["source"], f"{path}.source"),
        idempotency_key=_text(raw["idempotency_key"], f"{path}.idempotency_key"),
        value=_text(raw["value"], f"{path}.value"),
        evidence_text=_text(raw["evidence_text"], f"{path}.evidence_text"),
        reason=_text(raw["reason"], f"{path}.reason"),
        occurred_at=_datetime(raw["occurred_at"], f"{path}.occurred_at"),
        enforce_expected_revision=enforce_expected_revision,
        expected_sequence=sequence,
        expected_idempotent_replay=idempotent,
    )


def _parse_recall(
    raw: dict[str, object],
    index: int,
    recall_k: int,
) -> RecallReplayCase:
    path = f"dataset.cases[{index}]"
    required = {"type", "id", "scope", "query", "context", "limit", "expected"}
    _fields(
        raw,
        path=path,
        required=required,
        allowed=required | {"valid_at", "known_at"},
    )
    limit = _integer(raw["limit"], f"{path}.limit")
    if limit < recall_k:
        raise DatasetValidationError(f"{path}.limit must be at least dataset.recall_k")
    expected = _object(raw["expected"], f"{path}.expected")
    _fields(
        expected,
        path=f"{path}.expected",
        required={"relevant", "forbidden", "abstain"},
        allowed={"relevant", "forbidden", "abstain"},
    )
    abstain = expected["abstain"]
    if not isinstance(abstain, bool):
        raise DatasetValidationError(f"{path}.expected.abstain must be a boolean")
    relevant = tuple(
        _reference(item, f"{path}.expected.relevant[{item_index}]")
        for item_index, item in enumerate(_array(expected["relevant"], f"{path}.expected.relevant"))
    )
    forbidden = tuple(
        _reference(item, f"{path}.expected.forbidden[{item_index}]")
        for item_index, item in enumerate(
            _array(expected["forbidden"], f"{path}.expected.forbidden")
        )
    )
    if not relevant and not forbidden and not abstain:
        raise DatasetValidationError(f"{path}.expected must contain an evaluation label")
    return RecallReplayCase(
        case_id=_text(raw["id"], f"{path}.id"),
        command=RecallMemory(
            scope=_scope(raw["scope"], f"{path}.scope"),
            query=_text(raw["query"], f"{path}.query"),
            context=_context(raw["context"], f"{path}.context"),
            limit=limit,
            valid_at=_optional_datetime(raw.get("valid_at"), f"{path}.valid_at"),
            known_at=_optional_datetime(raw.get("known_at"), f"{path}.known_at"),
        ),
        relevant=relevant,
        forbidden=forbidden,
        expect_abstention=abstain,
    )


def _preference_expectation(raw: object, path: str) -> tuple[int | None, bool | None]:
    if raw is None:
        return None, None
    value = _object(raw, path)
    _fields(
        value,
        path=path,
        required=set(),
        allowed={"sequence", "idempotent_replay"},
    )
    sequence = value.get("sequence")
    idempotent = value.get("idempotent_replay")
    parsed_sequence = _integer(sequence, f"{path}.sequence") if sequence is not None else None
    if idempotent is not None and not isinstance(idempotent, bool):
        raise DatasetValidationError(f"{path}.idempotent_replay must be a boolean")
    return parsed_sequence, idempotent


def _reference(raw: object, path: str) -> MemoryReference:
    value = _object(raw, path)
    _fields(
        value,
        path=path,
        required=set(),
        allowed={"case_id", "record_id", "revision_id"},
    )
    case_id = value.get("case_id")
    record_id = value.get("record_id")
    revision_id = value.get("revision_id")
    if case_id is not None:
        if record_id is not None or revision_id is not None:
            raise DatasetValidationError(f"{path} must use a case_id or a UUID pair")
        return MemoryReference.from_case(_text(case_id, f"{path}.case_id"))
    if record_id is None or revision_id is None:
        raise DatasetValidationError(f"{path} must contain case_id or both UUID fields")
    return MemoryReference.from_ids(
        _uuid(record_id, f"{path}.record_id"),
        _uuid(revision_id, f"{path}.revision_id"),
    )


def _scope(raw: object, path: str) -> Scope:
    value = _object(raw, path)
    _fields(
        value,
        path=path,
        required={"tenant_id", "subject_id"},
        allowed={"tenant_id", "subject_id"},
    )
    return Scope(
        tenant_id=_text(value["tenant_id"], f"{path}.tenant_id"),
        subject_id=_text(value["subject_id"], f"{path}.subject_id"),
    )


def _context(raw: object, path: str) -> ContextSignature:
    value = _object(raw, path)
    facets: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str):
            raise DatasetValidationError(f"{path} values must be strings")
        facets[key] = item
    return ContextSignature.from_mapping(facets)


def _datetime(raw: object, path: str) -> datetime:
    value = _text(raw, path)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise DatasetValidationError(f"{path} must be an RFC 3339 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DatasetValidationError(f"{path} must include a UTC offset")
    return parsed


def _optional_datetime(raw: object, path: str) -> datetime | None:
    return None if raw is None else _datetime(raw, path)


def _uuid(raw: object, path: str) -> UUID:
    value = _text(raw, path)
    try:
        return UUID(value)
    except ValueError as error:
        raise DatasetValidationError(f"{path} must be a UUID") from error


def _fields(
    value: Mapping[str, object],
    *,
    path: str,
    required: set[str],
    allowed: set[str],
) -> None:
    missing = required - value.keys()
    if missing:
        raise DatasetValidationError(f"{path} is missing required fields")
    unknown = value.keys() - allowed
    if unknown:
        raise DatasetValidationError(f"{path} contains unknown fields")


def _object(raw: object, path: str) -> dict[str, object]:
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise DatasetValidationError(f"{path} must be an object")
    return cast(dict[str, object], raw)


def _array(raw: object, path: str) -> list[object]:
    if not isinstance(raw, list):
        raise DatasetValidationError(f"{path} must be an array")
    return cast(list[object], raw)


def _text(raw: object, path: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise DatasetValidationError(f"{path} must be a non-blank string")
    return raw


def _integer(raw: object, path: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise DatasetValidationError(f"{path} must be an integer")
    return raw


def _number(raw: object, path: str) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise DatasetValidationError(f"{path} must be a number")
    value = float(raw)
    if not isfinite(value):
        raise DatasetValidationError(f"{path} must be finite")
    return value
