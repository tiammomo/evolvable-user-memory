from __future__ import annotations

import logging
from datetime import datetime
from hashlib import sha256
from threading import Lock
from typing import Any, Protocol

from evolvable_memory.application.ports import EmbeddingPort
from evolvable_memory.application.projection_types import (
    ProjectionDocument,
    ProjectionHit,
    ProjectionSearchResult,
)
from evolvable_memory.domain.common import Scope

logger = logging.getLogger("evolvable_memory.projection")


class _MilvusClientLike(Protocol):
    def has_collection(self, collection_name: str, **kwargs: object) -> bool: ...

    def create_collection(self, **kwargs: object) -> None: ...

    def create_schema(self, **kwargs: object) -> Any: ...

    def prepare_index_params(self, **kwargs: object) -> Any: ...

    def load_collection(self, collection_name: str, **kwargs: object) -> None: ...

    def describe_collection(self, collection_name: str, **kwargs: object) -> dict[str, object]: ...

    def list_collections(self, **kwargs: object) -> list[str]: ...

    def upsert(
        self, collection_name: str, data: list[dict[str, object]], **kwargs: object
    ) -> Any: ...

    def search(self, collection_name: str, data: list[list[float]], **kwargs: object) -> Any: ...

    def delete(self, collection_name: str, **kwargs: object) -> Any: ...

    def drop_collection(self, collection_name: str, **kwargs: object) -> None: ...

    def close(self) -> None: ...


class MilvusMemoryProjection:
    """Disposable vector projection; PostgreSQL remains the recall authority."""

    def __init__(
        self,
        *,
        uri: str,
        collection_name: str,
        embedder: EmbeddingPort,
        token: str | None = None,
        timeout_seconds: float = 5.0,
        consistency_level: str = "Bounded",
        min_similarity: float = 0.0,
        client: _MilvusClientLike | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Milvus timeout must be positive")
        if not 0.0 <= min_similarity <= 1.0:
            raise ValueError("Milvus minimum similarity must be between 0 and 1")
        if consistency_level not in {"Strong", "Bounded", "Eventually", "Session"}:
            raise ValueError("unsupported Milvus consistency level")
        self._uri = uri
        self._token = token
        self._collection_name = collection_name
        self._embedder = embedder
        self._timeout_seconds = timeout_seconds
        self._consistency_level = consistency_level
        self._min_similarity = min_similarity
        self._client = client
        self._collection_ready = False
        self._lock = Lock()

    def close(self) -> None:
        client = self._client
        self._client = None
        self._collection_ready = False
        if client is not None:
            client.close()

    def is_ready(self) -> bool:
        try:
            client = self._connect()
            self._ensure_collection(client)
            client.list_collections(timeout=self._timeout_seconds)
            return True
        except Exception:
            return False

    def upsert(self, document: ProjectionDocument) -> None:
        client = self._connect()
        self._ensure_collection(client)
        vector = self._embedder.embed(document.search_text)
        client.upsert(
            collection_name=self._collection_name,
            data=[
                {
                    "projection_id": self._projection_id(document.revision_id),
                    "tenant_hash": _scope_hash(document.scope.tenant_id),
                    "subject_hash": _scope_hash(document.scope.subject_id),
                    "record_id": str(document.record_id),
                    "revision_id": str(document.revision_id),
                    "source_event_id": str(document.source_event_id),
                    "valid_from_us": _epoch_microseconds(document.valid_from.timestamp()),
                    "recorded_at_us": _epoch_microseconds(document.recorded_at.timestamp()),
                    "model_hash": self._model_hash,
                    "source_hash": sha256(document.search_text.encode("utf-8")).hexdigest(),
                    "dense_vector": list(vector),
                }
            ],
            timeout=self._timeout_seconds,
        )

    def search(
        self,
        scope: Scope,
        *,
        query: str,
        limit: int,
        valid_at: datetime,
        known_at: datetime,
    ) -> ProjectionSearchResult:
        if limit < 1:
            raise ValueError("projection search limit must be positive")
        try:
            client = self._connect()
            self._ensure_collection(client)
            vector = self._embedder.embed(query)
            expression = (
                f'tenant_hash == "{_scope_hash(scope.tenant_id)}" and '
                f'subject_hash == "{_scope_hash(scope.subject_id)}" and '
                f'model_hash == "{self._model_hash}" and '
                f"valid_from_us <= {_epoch_microseconds(valid_at.timestamp())} and "
                f"recorded_at_us <= {_epoch_microseconds(known_at.timestamp())}"
            )
            result = client.search(
                collection_name=self._collection_name,
                data=[list(vector)],
                anns_field="dense_vector",
                filter=expression,
                limit=limit,
                output_fields=["revision_id"],
                search_params={"metric_type": "COSINE", "params": {}},
                consistency_level=self._consistency_level,
                timeout=self._timeout_seconds,
            )
            hits = tuple(self._hits(result))
            return ProjectionSearchResult(hits=hits)
        except Exception as exc:
            logger.warning(
                "projection_search_unavailable",
                extra={"error_type": type(exc).__name__},
            )
            return ProjectionSearchResult(available=False, reason=type(exc).__name__)

    def delete_scope(self, scope: Scope) -> int:
        client = self._connect()
        self._ensure_collection(client)
        expression = (
            f'tenant_hash == "{_scope_hash(scope.tenant_id)}" and '
            f'subject_hash == "{_scope_hash(scope.subject_id)}"'
        )
        result = client.delete(
            collection_name=self._collection_name,
            filter=expression,
            timeout=self._timeout_seconds,
        )
        if isinstance(result, dict):
            raw_count = result.get("delete_count", 0)
            return int(raw_count) if isinstance(raw_count, int | float) else 0
        raw_count = getattr(result, "delete_count", 0)
        return int(raw_count) if isinstance(raw_count, int | float) else 0

    def reset(self) -> None:
        client = self._connect()
        with self._lock:
            if client.has_collection(self._collection_name, timeout=self._timeout_seconds):
                client.drop_collection(self._collection_name, timeout=self._timeout_seconds)
            self._collection_ready = False
        self._ensure_collection(client)

    @property
    def _model_hash(self) -> str:
        descriptor = f"{self._embedder.model_id}:{self._embedder.dimensions}"
        return sha256(descriptor.encode("utf-8")).hexdigest()

    def _projection_id(self, revision_id: object) -> str:
        descriptor = f"{revision_id}:{self._model_hash}"
        return sha256(descriptor.encode("utf-8")).hexdigest()

    def _connect(self) -> _MilvusClientLike:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                from pymilvus import MilvusClient

                self._client = MilvusClient(
                    uri=self._uri,
                    token=self._token or "",
                    timeout=self._timeout_seconds,
                )
        return self._client

    def _ensure_collection(self, client: _MilvusClientLike) -> None:
        if self._collection_ready:
            return
        with self._lock:
            if self._collection_ready:
                return
            if not client.has_collection(
                self._collection_name,
                timeout=self._timeout_seconds,
            ):
                self._create_collection(client)
            else:
                self._validate_collection(client)
            client.load_collection(
                self._collection_name,
                timeout=self._timeout_seconds,
            )
            self._collection_ready = True

    def _validate_collection(self, client: _MilvusClientLike) -> None:
        from pymilvus import DataType

        description = client.describe_collection(
            self._collection_name,
            timeout=self._timeout_seconds,
        )
        raw_fields = description.get("fields")
        if not isinstance(raw_fields, list):
            raise RuntimeError("Milvus collection schema is unavailable")
        fields = {
            field.get("name"): field
            for field in raw_fields
            if isinstance(field, dict) and isinstance(field.get("name"), str)
        }
        expected_types = {
            "projection_id": DataType.VARCHAR,
            "tenant_hash": DataType.VARCHAR,
            "subject_hash": DataType.VARCHAR,
            "record_id": DataType.VARCHAR,
            "revision_id": DataType.VARCHAR,
            "source_event_id": DataType.VARCHAR,
            "valid_from_us": DataType.INT64,
            "recorded_at_us": DataType.INT64,
            "model_hash": DataType.VARCHAR,
            "source_hash": DataType.VARCHAR,
            "dense_vector": DataType.FLOAT_VECTOR,
        }
        if any(
            fields.get(name, {}).get("type") != datatype
            for name, datatype in expected_types.items()
        ):
            raise RuntimeError("Milvus collection schema is incompatible")
        vector_params = fields["dense_vector"].get("params")
        if not isinstance(vector_params, dict) or int(vector_params.get("dim", 0)) != (
            self._embedder.dimensions
        ):
            raise RuntimeError("Milvus collection dimensions are incompatible")
        if not fields["projection_id"].get("is_primary") or not fields["tenant_hash"].get(
            "is_partition_key"
        ):
            raise RuntimeError("Milvus collection key schema is incompatible")

    def _create_collection(self, client: _MilvusClientLike) -> None:
        from pymilvus import DataType

        schema = client.create_schema(
            auto_id=False,
            enable_dynamic_field=False,
            partition_key_field="tenant_hash",
        )
        schema.add_field(
            field_name="projection_id",
            datatype=DataType.VARCHAR,
            is_primary=True,
            max_length=64,
        )
        schema.add_field(
            field_name="tenant_hash",
            datatype=DataType.VARCHAR,
            max_length=64,
        )
        schema.add_field(
            field_name="subject_hash",
            datatype=DataType.VARCHAR,
            max_length=64,
        )
        for field_name in ("record_id", "revision_id", "source_event_id"):
            schema.add_field(
                field_name=field_name,
                datatype=DataType.VARCHAR,
                max_length=36,
            )
        for field_name in ("valid_from_us", "recorded_at_us"):
            schema.add_field(field_name=field_name, datatype=DataType.INT64)
        for field_name in ("model_hash", "source_hash"):
            schema.add_field(
                field_name=field_name,
                datatype=DataType.VARCHAR,
                max_length=64,
            )
        schema.add_field(
            field_name="dense_vector",
            datatype=DataType.FLOAT_VECTOR,
            dim=self._embedder.dimensions,
        )
        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense_vector",
            index_type="AUTOINDEX",
            metric_type="COSINE",
        )
        client.create_collection(
            collection_name=self._collection_name,
            schema=schema,
            index_params=index_params,
            consistency_level=self._consistency_level,
            timeout=self._timeout_seconds,
        )

    def _hits(self, result: object) -> list[ProjectionHit]:
        if not isinstance(result, list) or not result or not isinstance(result[0], list):
            return []
        hits: list[ProjectionHit] = []
        seen: set[object] = set()
        for raw_hit in result[0]:
            if not isinstance(raw_hit, dict):
                continue
            entity = raw_hit.get("entity")
            if not isinstance(entity, dict):
                continue
            raw_revision_id = entity.get("revision_id")
            if not isinstance(raw_revision_id, str):
                continue
            try:
                from uuid import UUID

                revision_id = UUID(raw_revision_id)
                score = max(0.0, min(1.0, float(raw_hit.get("distance", 0.0))))
            except (TypeError, ValueError):
                continue
            if score < self._min_similarity or revision_id in seen:
                continue
            seen.add(revision_id)
            hits.append(ProjectionHit(revision_id=revision_id, score=score))
        return hits


def _scope_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _epoch_microseconds(timestamp: float) -> int:
    return round(timestamp * 1_000_000)
