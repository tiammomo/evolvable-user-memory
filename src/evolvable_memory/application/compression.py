from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from evolvable_memory.domain.common import DomainError
from evolvable_memory.domain.experience import RecalledItem, RecallTrace
from evolvable_memory.domain.projection import (
    ContextCompressionAlgorithm,
    ContextProjectionSegment,
    ContextProjectionSource,
    RecallContextProjection,
)


@dataclass(frozen=True, slots=True)
class _CandidateSegment:
    content: str
    sources: tuple[ContextProjectionSource, ...]


class RecallContextCompressor:
    """Build bounded JSON context without synthesizing or mutating memory facts."""

    _DOCUMENT_PREFIX = '{"memories":['
    _DOCUMENT_SUFFIX = "]}"

    def project(
        self,
        trace: RecallTrace,
        *,
        algorithm: ContextCompressionAlgorithm,
        budget_characters: int,
    ) -> RecallContextProjection:
        if not 64 <= budget_characters <= 100_000:
            raise DomainError("projection budget must be in [64, 100000] characters")

        ordered_items = tuple(sorted(trace.items, key=lambda item: item.rank))
        raw_candidates = tuple(self._candidate(item) for item in ordered_items)
        candidates = self._apply_algorithm(raw_candidates, algorithm)
        selected = self._select_within_budget(candidates, budget_characters)
        content = self._document(tuple(candidate.content for candidate in selected))
        segments = tuple(
            ContextProjectionSegment(content=candidate.content, sources=candidate.sources)
            for candidate in selected
        )
        included_count = sum(len(segment.sources) for segment in segments)
        original_content = self._document(tuple(candidate.content for candidate in raw_candidates))
        configuration_sha256 = self._digest(
            {
                "algorithm": algorithm.value,
                "algorithm_version": algorithm.version,
                "budget_characters": budget_characters,
            }
        )
        source_sha256 = self._digest(self._source_payload(trace, raw_candidates))
        projection_sha256 = self._digest(
            {
                "configuration_sha256": configuration_sha256,
                "source_sha256": source_sha256,
                "content": content,
                "segments": [
                    {
                        "content": segment.content,
                        "revision_ids": [str(source.revision_id) for source in segment.sources],
                    }
                    for segment in segments
                ],
            }
        )
        return RecallContextProjection(
            scope=trace.scope,
            trace_id=trace.id,
            policy_id=trace.policy_id,
            policy_version=trace.policy_version,
            algorithm=algorithm,
            budget_characters=budget_characters,
            valid_at=trace.valid_at,
            known_at=trace.known_at,
            trace_created_at=trace.created_at,
            content=content,
            segments=segments,
            source_item_count=len(ordered_items),
            omitted_item_count=len(ordered_items) - included_count,
            original_character_count=len(original_content),
            configuration_sha256=configuration_sha256,
            source_sha256=source_sha256,
            projection_sha256=projection_sha256,
        )

    def _candidate(self, item: RecalledItem) -> _CandidateSegment:
        content = json.dumps(
            {
                "context": item.context.as_dict(),
                "key": item.key,
                "value": item.value,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return _CandidateSegment(
            content=content,
            sources=(
                ContextProjectionSource(
                    record_id=item.record_id,
                    revision_id=item.revision_id,
                    rank=item.rank,
                    score=item.score,
                ),
            ),
        )

    def _apply_algorithm(
        self,
        candidates: tuple[_CandidateSegment, ...],
        algorithm: ContextCompressionAlgorithm,
    ) -> tuple[_CandidateSegment, ...]:
        if algorithm is ContextCompressionAlgorithm.RANKED_EXTRACTIVE:
            return candidates
        if algorithm is ContextCompressionAlgorithm.EXACT_DEDUPLICATED:
            deduplicated: dict[str, list[ContextProjectionSource]] = {}
            for candidate in candidates:
                deduplicated.setdefault(candidate.content, []).extend(candidate.sources)
            return tuple(
                _CandidateSegment(content=content, sources=tuple(sources))
                for content, sources in deduplicated.items()
            )
        raise DomainError("unsupported context compression algorithm")

    def _select_within_budget(
        self,
        candidates: tuple[_CandidateSegment, ...],
        budget_characters: int,
    ) -> tuple[_CandidateSegment, ...]:
        selected: list[_CandidateSegment] = []
        for candidate in candidates:
            proposed = self._document(
                (*(existing.content for existing in selected), candidate.content)
            )
            if len(proposed) <= budget_characters:
                selected.append(candidate)
        return tuple(selected)

    def _document(self, segments: tuple[str, ...]) -> str:
        return f"{self._DOCUMENT_PREFIX}{','.join(segments)}{self._DOCUMENT_SUFFIX}"

    def _source_payload(
        self,
        trace: RecallTrace,
        candidates: tuple[_CandidateSegment, ...],
    ) -> dict[str, object]:
        return {
            "trace_id": str(trace.id),
            "scope": {
                "tenant_id": trace.scope.tenant_id,
                "subject_id": trace.scope.subject_id,
            },
            "query": trace.query,
            "context": trace.context.as_dict(),
            "policy_id": str(trace.policy_id),
            "policy_version": trace.policy_version,
            "valid_at": trace.valid_at.isoformat(),
            "known_at": trace.known_at.isoformat(),
            "created_at": trace.created_at.isoformat(),
            "items": [
                {
                    "content": candidate.content,
                    "record_id": str(candidate.sources[0].record_id),
                    "revision_id": str(candidate.sources[0].revision_id),
                    "rank": candidate.sources[0].rank,
                    "score": candidate.sources[0].score,
                }
                for candidate in candidates
            ],
        }

    def _digest(self, payload: object) -> str:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()
