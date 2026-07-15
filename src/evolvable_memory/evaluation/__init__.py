"""Deterministic, privacy-preserving evaluation adapter."""

from evolvable_memory.evaluation.loader import (
    DatasetValidationError,
    list_builtin_datasets,
    load_dataset,
)

__all__ = [
    "DatasetValidationError",
    "list_builtin_datasets",
    "load_dataset",
]
