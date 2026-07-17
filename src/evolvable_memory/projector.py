from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
from threading import Event
from uuid import uuid4

from evolvable_memory.adapters.milvus import MilvusMemoryProjection
from evolvable_memory.adapters.projection_outbox import PostgresProjectionEventSource
from evolvable_memory.adapters.system import SystemClock
from evolvable_memory.application.projection import (
    MemoryProjectionWorker,
    ProjectionWorkerSettings,
)
from evolvable_memory.composition import build_recall_projection
from evolvable_memory.config import Settings
from evolvable_memory.domain.common import DomainError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project memory revisions into Milvus")
    parser.add_argument(
        "command",
        choices=("run", "once", "rebuild", "check"),
        nargs="?",
        default="run",
    )
    return parser


def _build_worker(settings: Settings) -> MemoryProjectionWorker:
    if settings.database_url is None:
        raise DomainError("EMF_DATABASE_URL is required by the projection worker")
    projection = build_recall_projection(settings)
    if not isinstance(projection, MilvusMemoryProjection):
        raise DomainError("EMF_PROJECTION_MODE=milvus is required by the projection worker")
    source = PostgresProjectionEventSource(
        settings.database_url,
        min_size=1,
        max_size=min(4, settings.database_pool_max_size),
        readiness_timeout=settings.database_readiness_timeout_seconds,
    )
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"
    return MemoryProjectionWorker(
        source=source,
        sink=projection,
        clock=SystemClock(),
        worker_id=worker_id,
        settings=ProjectionWorkerSettings(
            projection_name=settings.projection_name,
            batch_size=settings.projection_worker_batch_size,
            lease_seconds=settings.projection_worker_lease_seconds,
            retry_base_seconds=settings.projection_worker_retry_base_seconds,
            retry_max_seconds=settings.projection_worker_retry_max_seconds,
            max_attempts=settings.projection_worker_max_attempts,
        ),
    )


def run() -> None:
    args = _parser().parse_args()
    settings = Settings.from_environment()
    logging.basicConfig(level=settings.log_level, format="%(message)s")
    worker = _build_worker(settings)
    try:
        if args.command == "check":
            if not worker.is_ready():
                raise SystemExit(1)
            return
        if args.command == "rebuild":
            print(json.dumps({"requeued": worker.rebuild()}))
            return
        if args.command == "once":
            print(json.dumps(_result_dict(worker.run_once())))
            return
        _run_forever(worker, poll_seconds=settings.projection_worker_poll_seconds)
    finally:
        worker.close()


def _run_forever(worker: MemoryProjectionWorker, *, poll_seconds: float) -> None:
    stopped = Event()

    def stop(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopped.is_set():
        result = worker.run_once()
        if result.idle:
            stopped.wait(poll_seconds)


def _result_dict(result: object) -> dict[str, int]:
    from evolvable_memory.application.projection_types import ProjectionRunResult

    if not isinstance(result, ProjectionRunResult):
        raise TypeError("unexpected projection result")
    return {
        "claimed": result.claimed,
        "succeeded": result.succeeded,
        "failed": result.failed,
        "dead_lettered": result.dead_lettered,
    }


if __name__ == "__main__":
    run()
