from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)


class Uuid4Generator:
    def new(self) -> UUID:
        return uuid4()
