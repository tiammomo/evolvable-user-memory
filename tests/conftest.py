from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from evolvable_memory.adapters.in_memory import InMemoryMemoryStore
from evolvable_memory.application.service import MemoryApplication


@dataclass
class FixedClock:
    current: datetime = datetime(2026, 7, 14, 4, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, **parts: float) -> None:
        self.current += timedelta(**parts)


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
