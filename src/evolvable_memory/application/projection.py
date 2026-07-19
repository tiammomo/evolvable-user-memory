from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from evolvable_memory.application.ports import (
    Clock,
    PrivacyGovernancePort,
    ProjectionEventSourcePort,
    ProjectionSinkPort,
)
from evolvable_memory.application.projection_types import ProjectionRunResult

logger = logging.getLogger("evolvable_memory.projection")


@dataclass(frozen=True, slots=True)
class ProjectionWorkerSettings:
    projection_name: str = "milvus-memory-v1"
    batch_size: int = 64
    lease_seconds: float = 60.0
    retry_base_seconds: float = 2.0
    retry_max_seconds: float = 300.0
    max_attempts: int = 8

    def __post_init__(self) -> None:
        if not self.projection_name.strip():
            raise ValueError("projection_name must not be blank")
        if self.batch_size < 1:
            raise ValueError("batch_size must be positive")
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if self.retry_base_seconds <= 0:
            raise ValueError("retry_base_seconds must be positive")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("retry_max_seconds must be >= retry_base_seconds")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be positive")


class MemoryProjectionWorker:
    def __init__(
        self,
        *,
        source: ProjectionEventSourcePort,
        sink: ProjectionSinkPort,
        clock: Clock,
        worker_id: str,
        settings: ProjectionWorkerSettings,
        governance: PrivacyGovernancePort | None = None,
    ) -> None:
        if not worker_id.strip():
            raise ValueError("worker_id must not be blank")
        self._source = source
        self._sink = sink
        self._clock = clock
        self._worker_id = worker_id
        self._settings = settings
        self._governance = governance

    def is_ready(self) -> bool:
        return self._source.is_ready() and self._sink.is_ready()

    def rebuild(self) -> int:
        now = self._clock.now()
        self._sink.reset()
        self._source.discover(self._settings.projection_name)
        return self._source.requeue_all(self._settings.projection_name, at=now)

    def run_once(self) -> ProjectionRunResult:
        now = self._clock.now()
        self._source.discover(self._settings.projection_name)
        items = self._source.claim(
            self._settings.projection_name,
            worker_id=self._worker_id,
            limit=self._settings.batch_size,
            lease_until=now + timedelta(seconds=self._settings.lease_seconds),
        )
        succeeded = 0
        failed = 0
        dead_lettered = 0
        for item in items:
            try:
                document = self._source.load_document(item)
                if self._governance is None:
                    self._sink.upsert(document)
                else:
                    with self._governance.projection_context(
                        document.scope,
                        at=self._clock.now(),
                    ):
                        self._sink.upsert(document)
                self._source.complete(
                    self._settings.projection_name,
                    item=item,
                    worker_id=self._worker_id,
                    completed_at=self._clock.now(),
                )
                succeeded += 1
            except Exception as exc:
                failed_at = self._clock.now()
                dead_letter = item.attempts >= self._settings.max_attempts
                delay = min(
                    self._settings.retry_max_seconds,
                    self._settings.retry_base_seconds * (2 ** (item.attempts - 1)),
                )
                self._source.fail(
                    self._settings.projection_name,
                    item=item,
                    worker_id=self._worker_id,
                    failed_at=failed_at,
                    retry_at=failed_at + timedelta(seconds=delay),
                    error=type(exc).__name__,
                    dead_letter=dead_letter,
                )
                failed += 1
                dead_lettered += int(dead_letter)
                logger.warning(
                    "projection_event_failed",
                    extra={
                        "event_id": str(item.event_id),
                        "attempts": item.attempts,
                        "dead_letter": dead_letter,
                        "error_type": type(exc).__name__,
                    },
                )
        return ProjectionRunResult(
            claimed=len(items),
            succeeded=succeeded,
            failed=failed,
            dead_lettered=dead_lettered,
        )

    def close(self) -> None:
        try:
            if self._governance is not None:
                self._governance.close()
        finally:
            self._source.close()
            self._sink.close()
