from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from evolvable_memory.application.commands import OutcomeResult, PreferenceResult
from evolvable_memory.domain.experience import OutcomeKind, RecallTrace, UtilityEstimate
from evolvable_memory.domain.memory import MemoryRevision, MemorySnapshot

Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
OutcomeWeight = Annotated[float, Field(gt=0.0, le=10.0)]


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreferenceWriteRequest(ApiModel):
    tenant_id: str = Field(min_length=1, description="开发环境租户作用域。")
    subject_id: str = Field(min_length=1, description="租户内的用户或主体标识。")
    source: str = Field(min_length=1, description="证据来源, 例如 conversation。")
    idempotency_key: str = Field(
        min_length=1,
        description="作用域内唯一的幂等键; 重试必须复用, 新的事实必须更换。",
    )
    key: str = Field(min_length=1, description="稳定的记忆键, 建议使用点分命名。")
    value: str = Field(min_length=1, description="根据证据得出的当前偏好值。")
    context: dict[str, str] = Field(
        default_factory=dict,
        description="偏好成立的上下文维度; 键和值都参与匹配。",
    )
    evidence_text: str = Field(min_length=1, description="用户或来源的原始证据文本。")
    confidence: Confidence = 0.80
    occurred_at: datetime | None = Field(
        default=None,
        description="证据实际发生时间; 省略时使用服务端当前 UTC 时间。",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "tenant_id": "demo",
                "subject_id": "alice",
                "source": "conversation",
                "idempotency_key": "turn-42:preference-1",
                "key": "drink.preference",
                "value": "decaf coffee",
                "context": {"time_of_day": "evening"},
                "evidence_text": "晚上我只喝低因咖啡",
                "confidence": 0.92,
            }
        },
    )


class PreferenceCorrectionRequest(ApiModel):
    tenant_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    value: str = Field(min_length=1)
    evidence_text: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    occurred_at: datetime | None = None

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "tenant_id": "demo",
                "subject_id": "alice",
                "source": "explicit-feedback",
                "idempotency_key": "turn-43:correction-1",
                "value": "herbal tea",
                "evidence_text": "其实晚上我改喝花草茶",
                "reason": "user corrected an outdated preference",
            }
        },
    )


class PreferenceResponse(ApiModel):
    observation_id: UUID
    candidate_id: UUID
    record_id: UUID
    revision_id: UUID
    sequence: int
    idempotent_replay: bool

    @classmethod
    def from_result(cls, result: PreferenceResult) -> PreferenceResponse:
        return cls(
            observation_id=result.observation_id,
            candidate_id=result.candidate_id,
            record_id=result.record_id,
            revision_id=result.revision_id,
            sequence=result.sequence,
            idempotent_replay=result.idempotent_replay,
        )


class PreferenceSummaryResponse(ApiModel):
    record_id: UUID
    revision_id: UUID
    sequence: int
    key: str
    value: str
    context: dict[str, str]
    confidence: float
    support_count: int
    evidence_count: int
    valid_from: datetime
    recorded_at: datetime

    @classmethod
    def from_snapshot(cls, snapshot: MemorySnapshot) -> PreferenceSummaryResponse:
        return cls(
            record_id=snapshot.record.id,
            revision_id=snapshot.revision.id,
            sequence=snapshot.revision.sequence,
            key=snapshot.record.key,
            value=snapshot.revision.value,
            context=snapshot.record.context.as_dict(),
            confidence=snapshot.revision.belief.confidence,
            support_count=snapshot.revision.belief.support_count,
            evidence_count=len(snapshot.revision.evidence_ids),
            valid_from=snapshot.revision.valid_from,
            recorded_at=snapshot.revision.recorded_at,
        )


class RecallRequest(ApiModel):
    tenant_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    context: dict[str, str] = Field(default_factory=dict)
    limit: int = Field(default=10, ge=1, le=100)

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "tenant_id": "demo",
                "subject_id": "alice",
                "query": "晚上应该准备什么饮料",
                "context": {"time_of_day": "evening"},
                "limit": 5,
            }
        },
    )


class ScoreResponse(ApiModel):
    semantic: float
    context: float
    belief: float
    utility: float
    recency: float


class RecallItemResponse(ApiModel):
    record_id: UUID
    revision_id: UUID
    key: str
    value: str
    context: dict[str, str]
    rank: int
    score: float
    breakdown: ScoreResponse
    evidence_ids: list[UUID]


class RecallResponse(ApiModel):
    trace_id: UUID
    policy_id: UUID
    policy_version: int
    created_at: datetime
    items: list[RecallItemResponse]

    @classmethod
    def from_trace(cls, trace: RecallTrace) -> RecallResponse:
        return cls(
            trace_id=trace.id,
            policy_id=trace.policy_id,
            policy_version=trace.policy_version,
            created_at=trace.created_at,
            items=[
                RecallItemResponse(
                    record_id=item.record_id,
                    revision_id=item.revision_id,
                    key=item.key,
                    value=item.value,
                    context=item.context.as_dict(),
                    rank=item.rank,
                    score=item.score,
                    breakdown=ScoreResponse(
                        semantic=item.breakdown.semantic,
                        context=item.breakdown.context,
                        belief=item.breakdown.belief,
                        utility=item.breakdown.utility,
                        recency=item.breakdown.recency,
                    ),
                    evidence_ids=list(item.evidence_ids),
                )
                for item in trace.items
            ],
        )


class OutcomeWriteRequest(ApiModel):
    tenant_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    trace_id: UUID
    revision_id: UUID
    kind: OutcomeKind
    idempotency_key: str = Field(min_length=1)
    occurred_at: datetime | None = None
    weight: OutcomeWeight = 1.0
    note: str | None = None

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "tenant_id": "demo",
                "subject_id": "alice",
                "trace_id": "00000000-0000-0000-0000-000000000101",
                "revision_id": "00000000-0000-0000-0000-000000000102",
                "kind": "helpful",
                "idempotency_key": "task-9:outcome-1",
                "weight": 1.0,
                "note": "The recommendation was accepted.",
            }
        },
    )


class UtilityResponse(ApiModel):
    revision_id: UUID
    context_fingerprint: str
    mean: float
    sample_weight: float
    positive_weight: float
    negative_weight: float
    last_outcome_at: datetime | None

    @classmethod
    def from_estimate(cls, estimate: UtilityEstimate) -> UtilityResponse:
        return cls(
            revision_id=estimate.revision_id,
            context_fingerprint=estimate.context_fingerprint,
            mean=estimate.mean,
            sample_weight=estimate.sample_weight,
            positive_weight=estimate.positive_weight,
            negative_weight=estimate.negative_weight,
            last_outcome_at=estimate.last_outcome_at,
        )


class OutcomeResponse(ApiModel):
    outcome_id: UUID
    idempotent_replay: bool
    utility: UtilityResponse

    @classmethod
    def from_result(cls, result: OutcomeResult) -> OutcomeResponse:
        return cls(
            outcome_id=result.outcome.id,
            idempotent_replay=result.idempotent_replay,
            utility=UtilityResponse.from_estimate(result.utility),
        )


class RevisionResponse(ApiModel):
    id: UUID
    sequence: int
    value: str
    confidence: float
    support_count: int
    contradiction_count: int
    evidence_ids: list[UUID]
    valid_from: datetime
    recorded_at: datetime
    supersedes_revision_id: UUID | None

    @classmethod
    def from_revision(cls, revision: MemoryRevision) -> RevisionResponse:
        return cls(
            id=revision.id,
            sequence=revision.sequence,
            value=revision.value,
            confidence=revision.belief.confidence,
            support_count=revision.belief.support_count,
            contradiction_count=revision.belief.contradiction_count,
            evidence_ids=list(revision.evidence_ids),
            valid_from=revision.valid_from,
            recorded_at=revision.recorded_at,
            supersedes_revision_id=revision.supersedes_revision_id,
        )


class ErrorResponse(ApiModel):
    error: str
    detail: str


class ServiceInfoResponse(ApiModel):
    name: str
    version: str
    status: str
    storage: str
    frontend_url: str
    documentation_url: str
    production_ready: bool
    notice: str
