from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg import Connection
from psycopg import Error as PsycopgError
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout

from evolvable_memory.application.projection_types import (
    ProjectionDocument,
    ProjectionWorkItem,
)
from evolvable_memory.domain.common import ContextSignature, Scope

DbRow = dict[str, Any]
DbConnection = Connection[DbRow]

_REVISION_EVENT_TYPES = ("memory.revision.created", "memory.revision.appended")


class PostgresProjectionEventSource:
    """Leased, idempotent Milvus jobs derived from the authoritative outbox."""

    def __init__(
        self,
        database_url: str,
        *,
        min_size: int = 1,
        max_size: int = 4,
        open_timeout: float = 10.0,
        readiness_timeout: float = 1.0,
    ) -> None:
        conninfo = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._pool: ConnectionPool[DbConnection] = ConnectionPool(
            conninfo,
            kwargs={"row_factory": dict_row},
            min_size=min_size,
            max_size=max_size,
            open=True,
            check=ConnectionPool.check_connection,
        )
        self._pool.wait(timeout=open_timeout)
        self._readiness_timeout = readiness_timeout

    def close(self) -> None:
        self._pool.close()

    def is_ready(self) -> bool:
        try:
            with self._pool.connection(timeout=self._readiness_timeout) as connection:
                connection.execute("SELECT 1 FROM projection_jobs LIMIT 1").fetchone()
            return True
        except (OSError, PoolClosed, PoolTimeout, PsycopgError):
            return False

    def discover(self, projection_name: str) -> int:
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO projection_jobs (
                    projection_name, event_id, status, attempts, available_at,
                    created_at, updated_at
                )
                SELECT %s, event.id, 'pending', 0, event.occurred_at,
                       CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                FROM outbox_events AS event
                WHERE event.aggregate_type = 'memory_revision'
                  AND event.event_type = ANY(%s)
                ON CONFLICT (projection_name, event_id) DO NOTHING
                """,
                (projection_name, list(_REVISION_EVENT_TYPES)),
            )
        return cursor.rowcount

    def claim(
        self,
        projection_name: str,
        *,
        worker_id: str,
        limit: int,
        lease_until: datetime,
    ) -> tuple[ProjectionWorkItem, ...]:
        with self._pool.connection() as connection, connection.transaction():
            rows = connection.execute(
                """
                WITH claimable AS (
                    SELECT job.projection_name, job.event_id
                    FROM projection_jobs AS job
                    JOIN outbox_events AS event ON event.id = job.event_id
                    WHERE job.projection_name = %s
                      AND (
                          (job.status IN ('pending', 'failed')
                           AND job.available_at <= CURRENT_TIMESTAMP)
                          OR (job.status = 'processing'
                              AND job.lease_until <= CURRENT_TIMESTAMP)
                      )
                    ORDER BY event.occurred_at, event.id
                    FOR UPDATE OF job SKIP LOCKED
                    LIMIT %s
                )
                UPDATE projection_jobs AS job
                SET status = 'processing', attempts = job.attempts + 1,
                    lease_owner = %s, lease_until = %s,
                    last_error = NULL, processed_at = NULL,
                    updated_at = CURRENT_TIMESTAMP
                FROM claimable, outbox_events AS event
                WHERE job.projection_name = claimable.projection_name
                  AND job.event_id = claimable.event_id
                  AND event.id = job.event_id
                RETURNING event.id AS event_id, event.event_type,
                          event.payload, event.occurred_at, job.attempts
                """,
                (projection_name, limit, worker_id, lease_until),
            ).fetchall()
        return tuple(self._work_item(row) for row in rows)

    def load_document(self, item: ProjectionWorkItem) -> ProjectionDocument:
        with self._pool.connection() as connection:
            row = connection.execute(
                """
                SELECT record.key, record.context, revision.value,
                       revision.valid_from, revision.recorded_at
                FROM memory_revisions AS revision
                JOIN memory_records AS record
                  ON record.id = revision.record_id
                 AND record.tenant_id = revision.tenant_id
                 AND record.subject_id = revision.subject_id
                WHERE revision.id = %s AND revision.record_id = %s
                  AND revision.tenant_id = %s AND revision.subject_id = %s
                """,
                (
                    item.revision_id,
                    item.record_id,
                    item.scope.tenant_id,
                    item.scope.subject_id,
                ),
            ).fetchone()
        if row is None:
            raise LookupError("projection source revision does not exist in scope")
        return ProjectionDocument(
            source_event_id=item.event_id,
            scope=item.scope,
            record_id=item.record_id,
            revision_id=item.revision_id,
            key=row["key"],
            value=row["value"],
            context=ContextSignature.from_mapping(row["context"]),
            valid_from=row["valid_from"],
            recorded_at=row["recorded_at"],
        )

    def complete(
        self,
        projection_name: str,
        *,
        item: ProjectionWorkItem,
        worker_id: str,
        completed_at: datetime,
    ) -> None:
        with self._pool.connection() as connection, connection.transaction():
            cursor = connection.execute(
                """
                UPDATE projection_jobs
                SET status = 'succeeded', lease_owner = NULL, lease_until = NULL,
                    processed_at = %s, updated_at = %s
                WHERE projection_name = %s AND event_id = %s
                  AND status = 'processing' AND lease_owner = %s
                """,
                (completed_at, completed_at, projection_name, item.event_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("projection job lease was lost before completion")
            connection.execute(
                """
                INSERT INTO projection_cursors (
                    projection_name, last_event_id, last_event_occurred_at, updated_at
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (projection_name) DO UPDATE
                SET last_event_id = EXCLUDED.last_event_id,
                    last_event_occurred_at = EXCLUDED.last_event_occurred_at,
                    updated_at = EXCLUDED.updated_at
                WHERE projection_cursors.last_event_occurred_at IS NULL
                   OR projection_cursors.last_event_occurred_at
                      <= EXCLUDED.last_event_occurred_at
                """,
                (projection_name, item.event_id, item.occurred_at, completed_at),
            )

    def fail(
        self,
        projection_name: str,
        *,
        item: ProjectionWorkItem,
        worker_id: str,
        failed_at: datetime,
        retry_at: datetime,
        error: str,
        dead_letter: bool,
    ) -> None:
        status = "dead_letter" if dead_letter else "failed"
        with self._pool.connection() as connection:
            cursor = connection.execute(
                """
                UPDATE projection_jobs
                SET status = %s, available_at = %s,
                    lease_owner = NULL, lease_until = NULL,
                    last_error = %s, processed_at = NULL, updated_at = %s
                WHERE projection_name = %s AND event_id = %s
                  AND status = 'processing' AND lease_owner = %s
                """,
                (
                    status,
                    retry_at,
                    error[:200],
                    failed_at,
                    projection_name,
                    item.event_id,
                    worker_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("projection job lease was lost before failure recording")

    def requeue_all(self, projection_name: str, *, at: datetime) -> int:
        with self._pool.connection() as connection, connection.transaction():
            cursor = connection.execute(
                """
                UPDATE projection_jobs
                SET status = 'pending', attempts = 0, available_at = %s,
                    lease_owner = NULL, lease_until = NULL, last_error = NULL,
                    processed_at = NULL, updated_at = %s
                WHERE projection_name = %s
                """,
                (at, at, projection_name),
            )
            connection.execute(
                "DELETE FROM projection_cursors WHERE projection_name = %s",
                (projection_name,),
            )
        return cursor.rowcount

    @staticmethod
    def _work_item(row: DbRow) -> ProjectionWorkItem:
        payload = _mapping(row["payload"])
        return ProjectionWorkItem(
            event_id=row["event_id"],
            event_type=row["event_type"],
            scope=Scope(str(payload["tenant_id"]), str(payload["subject_id"])),
            record_id=UUID(str(payload["record_id"])),
            revision_id=UUID(str(payload["revision_id"])),
            occurred_at=row["occurred_at"],
            attempts=row["attempts"],
        )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("projection event payload must be an object")
    return value
