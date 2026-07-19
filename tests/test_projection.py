from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID

import pytest

from conftest import FixedClock, SequentialIds
from evolvable_memory.adapters.embeddings import HashingEmbedder
from evolvable_memory.adapters.in_memory import InMemoryMemoryStore
from evolvable_memory.adapters.milvus import MilvusMemoryProjection
from evolvable_memory.application.commands import RecallMemory, RememberPreference
from evolvable_memory.application.projection import (
    MemoryProjectionWorker,
    ProjectionWorkerSettings,
)
from evolvable_memory.application.projection_types import (
    ProjectionDocument,
    ProjectionHit,
    ProjectionSearchResult,
    ProjectionWorkItem,
)
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.domain.common import ContextSignature, Scope

NOW = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
ALICE = Scope("tenant-a", "alice")


def test_hash_embedding_is_deterministic_normalized_and_bounded() -> None:
    embedder = HashingEmbedder(dimensions=64)

    first = embedder.embed("decaf coffee in the evening")
    replay = embedder.embed("decaf coffee in the evening")
    different = embedder.embed("herbal tea")

    assert first == replay
    assert first != different
    assert len(first) == 64
    assert math.sqrt(sum(value * value for value in first)) == pytest.approx(1.0)


class _FakeSchema:
    def __init__(self) -> None:
        self.fields: list[tuple[str, object, dict[str, object]]] = []

    def add_field(self, field_name: str, datatype: object, **kwargs: object) -> _FakeSchema:
        self.fields.append((field_name, datatype, kwargs))
        return self


class _FakeIndexes:
    def __init__(self) -> None:
        self.indexes: list[tuple[str, dict[str, object]]] = []

    def add_index(self, field_name: str, **kwargs: object) -> None:
        self.indexes.append((field_name, kwargs))


class _FakeMilvusClient:
    def __init__(self) -> None:
        self.exists = False
        self.closed = False
        self.schema: _FakeSchema | None = None
        self.upserts: list[dict[str, object]] = []
        self.search_filter = ""
        self.search_result: object = [[]]
        self.fail_search = False
        self.dimensions = 64
        self.delete_filter = ""

    def has_collection(self, _collection_name: str, **_kwargs: object) -> bool:
        return self.exists

    def create_schema(self, **_kwargs: object) -> _FakeSchema:
        return _FakeSchema()

    def prepare_index_params(self, **_kwargs: object) -> _FakeIndexes:
        return _FakeIndexes()

    def create_collection(self, **kwargs: object) -> None:
        self.exists = True
        self.schema = kwargs["schema"]  # type: ignore[assignment]

    def load_collection(self, _collection_name: str, **_kwargs: object) -> None:
        return None

    def list_collections(self, **_kwargs: object) -> list[str]:
        return ["memory"] if self.exists else []

    def describe_collection(
        self,
        _collection_name: str,
        **_kwargs: object,
    ) -> dict[str, object]:
        from pymilvus import DataType

        fields: list[dict[str, object]] = [
            {"name": "projection_id", "type": DataType.VARCHAR, "is_primary": True},
            {"name": "tenant_hash", "type": DataType.VARCHAR, "is_partition_key": True},
            {"name": "subject_hash", "type": DataType.VARCHAR},
            {"name": "record_id", "type": DataType.VARCHAR},
            {"name": "revision_id", "type": DataType.VARCHAR},
            {"name": "source_event_id", "type": DataType.VARCHAR},
            {"name": "valid_from_us", "type": DataType.INT64},
            {"name": "recorded_at_us", "type": DataType.INT64},
            {"name": "model_hash", "type": DataType.VARCHAR},
            {"name": "source_hash", "type": DataType.VARCHAR},
            {
                "name": "dense_vector",
                "type": DataType.FLOAT_VECTOR,
                "params": {"dim": self.dimensions},
            },
        ]
        return {"fields": fields}

    def upsert(
        self,
        collection_name: str,
        data: list[dict[str, object]],
        **_kwargs: object,
    ) -> None:
        assert collection_name == "memory"
        self.upserts.extend(data)

    def search(
        self,
        collection_name: str,
        data: list[list[float]],
        **kwargs: object,
    ) -> object:
        assert collection_name == "memory"
        assert data
        if self.fail_search:
            raise OSError("milvus unavailable")
        self.search_filter = str(kwargs["filter"])
        return self.search_result

    def delete(self, collection_name: str, **kwargs: object) -> dict[str, int]:
        assert collection_name == "memory"
        self.delete_filter = str(kwargs["filter"])
        return {"delete_count": 2}

    def drop_collection(self, _collection_name: str, **_kwargs: object) -> None:
        self.exists = False

    def close(self) -> None:
        self.closed = True


def _document() -> ProjectionDocument:
    return ProjectionDocument(
        source_event_id=UUID(int=1),
        scope=ALICE,
        record_id=UUID(int=2),
        revision_id=UUID(int=3),
        key="drink.preference",
        value="secret decaf coffee",
        context=ContextSignature.from_mapping({"time_of_day": "evening"}),
        valid_from=NOW,
        recorded_at=NOW,
    )


def test_milvus_projection_stores_no_raw_memory_and_filters_scope() -> None:
    client = _FakeMilvusClient()
    client.search_result = [[{"distance": 0.87, "entity": {"revision_id": str(UUID(int=3))}}]]
    projection = MilvusMemoryProjection(
        uri="http://milvus:19530",
        collection_name="memory",
        embedder=HashingEmbedder(dimensions=64),
        min_similarity=0.15,
        client=client,
    )

    projection.upsert(_document())
    result = projection.search(
        ALICE,
        query="coffee",
        limit=5,
        valid_at=NOW,
        known_at=NOW,
    )

    assert result == ProjectionSearchResult(hits=(ProjectionHit(UUID(int=3), 0.87),))
    assert client.schema is not None
    assert "tenant_hash" in {field[0] for field in client.schema.fields}
    assert "tenant-a" not in client.search_filter
    assert "alice" not in client.search_filter
    assert "valid_from_us <=" in client.search_filter
    assert "recorded_at_us <=" in client.search_filter
    assert all("secret decaf coffee" not in str(value) for value in client.upserts[0].values())
    assert "value" not in client.upserts[0]
    assert "key" not in client.upserts[0]

    assert projection.delete_scope(ALICE) == 2
    assert "tenant-a" not in client.delete_filter
    assert "alice" not in client.delete_filter
    assert "tenant_hash" in client.delete_filter
    assert "subject_hash" in client.delete_filter


def test_milvus_projection_failure_is_an_explicit_fallback_signal() -> None:
    client = _FakeMilvusClient()
    client.exists = True
    client.fail_search = True
    projection = MilvusMemoryProjection(
        uri="http://milvus:19530",
        collection_name="memory",
        embedder=HashingEmbedder(dimensions=64),
        client=client,
    )

    result = projection.search(
        ALICE,
        query="coffee",
        limit=5,
        valid_at=NOW,
        known_at=NOW,
    )

    assert result.available is False
    assert result.reason == "OSError"
    assert result.hits == ()


def test_milvus_projection_rejects_an_incompatible_existing_collection() -> None:
    client = _FakeMilvusClient()
    client.exists = True
    client.dimensions = 32
    projection = MilvusMemoryProjection(
        uri="http://milvus:19530",
        collection_name="memory",
        embedder=HashingEmbedder(dimensions=64),
        client=client,
    )

    assert projection.is_ready() is False
    result = projection.search(
        ALICE,
        query="coffee",
        limit=5,
        valid_at=NOW,
        known_at=NOW,
    )
    assert result.available is False
    assert result.reason == "RuntimeError"


class _RecallProjection:
    def __init__(self) -> None:
        self.hits: tuple[ProjectionHit, ...] = ()
        self.available = True
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def is_ready(self) -> bool:
        return self.available

    def search(
        self,
        _scope: Scope,
        *,
        query: str,
        limit: int,
        valid_at: datetime,
        known_at: datetime,
    ) -> ProjectionSearchResult:
        assert query
        assert limit == 30
        assert valid_at == known_at == NOW
        return ProjectionSearchResult(hits=self.hits, available=self.available)


def test_hybrid_recall_revalidates_vector_candidates_against_authority() -> None:
    store = InMemoryMemoryStore()
    clock = FixedClock(NOW)
    projection = _RecallProjection()
    app = MemoryApplication(
        store=store,
        clock=clock,
        ids=SequentialIds(),
        recall_projection=projection,
        projection_search_oversample=10,
    )
    remembered = app.remember_preference(
        RememberPreference(
            scope=ALICE,
            source="conversation",
            idempotency_key="projection-memory",
            key="travel.style",
            value="quiet countryside stays",
            context=ContextSignature(),
            evidence_text="I prefer quiet countryside stays",
            confidence=0.9,
            occurred_at=NOW,
        )
    )
    projection.hits = (ProjectionHit(remembered.revision_id, 0.91),)

    trace = app.recall(
        RecallMemory(
            scope=ALICE,
            query="remote peaceful lodging",
            context=ContextSignature(),
            limit=3,
        )
    )
    cross_scope = app.recall(
        RecallMemory(
            scope=Scope("tenant-a", "bob"),
            query="remote peaceful lodging",
            context=ContextSignature(),
            limit=3,
        )
    )

    assert [item.revision_id for item in trace.items] == [remembered.revision_id]
    assert trace.items[0].breakdown.lexical == 0.0
    assert trace.items[0].breakdown.vector == 0.91
    assert cross_scope.items == ()
    assert store.utility_for(ALICE, remembered.revision_id, ContextSignature()).sample_weight == 0


class _ProjectionSource:
    def __init__(self, item: ProjectionWorkItem, document: ProjectionDocument) -> None:
        self.item = item
        self.document = document
        self.claimed = False
        self.completed: list[UUID] = []
        self.failures: list[tuple[str, bool]] = []

    def close(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    def discover(self, _projection_name: str) -> int:
        return 1

    def claim(self, _projection_name: str, **_kwargs: object) -> tuple[ProjectionWorkItem, ...]:
        if self.claimed:
            return ()
        self.claimed = True
        return (self.item,)

    def load_document(self, _item: ProjectionWorkItem) -> ProjectionDocument:
        return self.document

    def complete(self, _projection_name: str, **kwargs: object) -> None:
        item = kwargs["item"]
        assert isinstance(item, ProjectionWorkItem)
        self.completed.append(item.event_id)

    def fail(self, _projection_name: str, **kwargs: object) -> None:
        self.failures.append((str(kwargs["error"]), bool(kwargs["dead_letter"])))

    def requeue_all(self, _projection_name: str, **_kwargs: object) -> int:
        return 1


class _ProjectionSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.documents: list[ProjectionDocument] = []

    def close(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    def upsert(self, document: ProjectionDocument) -> None:
        if self.fail:
            raise OSError("provider details must not be persisted")
        self.documents.append(document)

    def reset(self) -> None:
        self.documents.clear()


def test_projection_worker_completes_and_dead_letters_without_error_content() -> None:
    item = ProjectionWorkItem(
        event_id=UUID(int=1),
        event_type="memory.revision.created",
        scope=ALICE,
        record_id=UUID(int=2),
        revision_id=UUID(int=3),
        occurred_at=NOW,
        attempts=2,
    )
    source = _ProjectionSource(item, _document())
    sink = _ProjectionSink(fail=True)
    worker = MemoryProjectionWorker(
        source=source,
        sink=sink,
        clock=FixedClock(NOW),
        worker_id="worker-1",
        settings=ProjectionWorkerSettings(max_attempts=2),
    )

    result = worker.run_once()

    assert result.claimed == result.failed == result.dead_lettered == 1
    assert result.succeeded == 0
    assert source.failures == [("OSError", True)]
    assert "provider details" not in source.failures[0][0]
