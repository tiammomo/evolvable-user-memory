from __future__ import annotations

from evolvable_memory.adapters.embeddings import HashingEmbedder, OpenAICompatibleEmbedder
from evolvable_memory.adapters.milvus import MilvusMemoryProjection
from evolvable_memory.application.ports import EmbeddingPort, RecallProjectionPort
from evolvable_memory.config import Settings


def build_embedder(settings: Settings) -> EmbeddingPort:
    if settings.embedding_provider == "openai_compatible":
        return OpenAICompatibleEmbedder(
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            api_key=settings.embedding_api_key,
            timeout_seconds=settings.embedding_timeout_seconds,
        )
    return HashingEmbedder(dimensions=settings.embedding_dimensions)


def build_recall_projection(settings: Settings) -> RecallProjectionPort | None:
    if settings.projection_mode == "disabled":
        return None
    return MilvusMemoryProjection(
        uri=settings.milvus_uri,
        token=settings.milvus_token,
        collection_name=settings.milvus_collection,
        embedder=build_embedder(settings),
        timeout_seconds=settings.milvus_timeout_seconds,
        consistency_level=settings.milvus_consistency_level,
        min_similarity=settings.milvus_min_similarity,
    )
