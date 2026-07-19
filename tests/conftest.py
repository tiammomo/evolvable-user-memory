from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from evolvable_memory.adapters.in_memory import InMemoryMemoryStore
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.migrate import upgrade_database

_DESTRUCTIVE_DATABASE_OPT_IN = "EMF_ALLOW_DESTRUCTIVE_TEST_DATABASE"


def _destructive_test_conninfo(database_url: str) -> str:
    conninfo = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    database_name = conninfo_to_dict(conninfo).get("dbname", "")
    if not database_name.lower().endswith("_test"):
        raise RuntimeError("destructive PostgreSQL tests require a database name ending in '_test'")
    if os.getenv(_DESTRUCTIVE_DATABASE_OPT_IN) != "1":
        raise RuntimeError(f"destructive PostgreSQL tests require {_DESTRUCTIVE_DATABASE_OPT_IN}=1")
    return conninfo


def prepare_postgres_database(database_url: str) -> None:
    """Migrate and clear an explicitly configured disposable test database."""
    conninfo = _destructive_test_conninfo(database_url)
    upgrade_database(database_url)
    with psycopg.connect(conninfo) as connection:
        connection.execute(
            """
            TRUNCATE TABLE
                authorization_audit_events, erasure_requests, suppression_fences,
                processing_grants,
                evolution_experiment_transitions, strategy_activations,
                evolution_experiments, outbox_events, utility_estimates,
                outcomes, memory_usage_items, memory_usages, recall_trace_items,
                recall_traces, revision_transitions, candidates, memory_revisions,
                memory_records, evidence_spans, observations, strategy_snapshots
            CASCADE
            """
        )


@dataclass
class FixedClock:
    current: datetime = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, **parts: float) -> None:
        self.current += timedelta(**parts)

    def set(self, value: datetime) -> None:
        self.current = value


class SequentialIds:
    def __init__(self) -> None:
        self._value = 0

    def new(self) -> UUID:
        self._value += 1
        return UUID(int=self._value)


@dataclass
class Harness:
    app: MemoryApplication
    store: InMemoryMemoryStore
    clock: FixedClock


@pytest.fixture
def harness() -> Harness:
    store = InMemoryMemoryStore()
    clock = FixedClock()
    return Harness(
        app=MemoryApplication(store=store, clock=clock, ids=SequentialIds()),
        store=store,
        clock=clock,
    )
