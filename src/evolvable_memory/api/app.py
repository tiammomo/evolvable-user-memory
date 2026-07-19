from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from threading import Lock
from typing import Any
from uuid import UUID

from fastapi import Depends, FastAPI, Request, Response, Security, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.types import ASGIApp, Receive, Send
from starlette.types import Scope as AsgiScope

from evolvable_memory import __version__
from evolvable_memory.adapters.authorization import (
    LoggingAuthorizationAuditSink,
    PostgresAuthorizationAuditSink,
    RolePolicyAuthorizer,
)
from evolvable_memory.adapters.in_memory import InMemoryMemoryStore
from evolvable_memory.adapters.in_memory_governance import DevelopmentBypassPrivacyGovernance
from evolvable_memory.adapters.postgres import PostgresMemoryStore
from evolvable_memory.adapters.postgres_governance import PostgresPrivacyGovernance
from evolvable_memory.adapters.system import SystemClock, Uuid4Generator
from evolvable_memory.api.contract import API_CAPABILITIES, API_CONTRACT, production_blockers
from evolvable_memory.api.middleware import ApiRuntimeMiddleware, RequestBodyTooLargeError
from evolvable_memory.api.schemas import (
    ErasureResponse,
    ErasureWriteRequest,
    ErrorResponse,
    MemoryUsageResponse,
    MemoryUsageWriteRequest,
    OutcomeResponse,
    OutcomeWriteRequest,
    PreferenceCorrectionRequest,
    PreferenceResponse,
    PreferenceSummaryResponse,
    PreferenceWriteRequest,
    ProcessingGrantResponse,
    ProcessingGrantRevocationRequest,
    ProcessingGrantWriteRequest,
    PurposeText,
    ReadinessResponse,
    RecallContextProjectionRequest,
    RecallContextProjectionResponse,
    RecallRequest,
    RecallResponse,
    RevisionResponse,
    ScopeId,
    ServiceInfoResponse,
    SuppressionRequest,
    SuppressionResponse,
)
from evolvable_memory.api.security import (
    DevelopmentIdentityResolver,
    IdentityResolver,
    JwtIdentityResolver,
    PyJwkSigningKeyProvider,
)
from evolvable_memory.application.access import AuthorizedMemoryApplication
from evolvable_memory.application.commands import (
    CorrectPreference,
    ProjectRecallContext,
    RecallMemory,
    RecordMemoryUsage,
    RecordOutcome,
    RememberPreference,
)
from evolvable_memory.application.governance import (
    EraseSubject,
    IssueProcessingGrant,
    PrivacyApplication,
    RevokeProcessingGrant,
    SuppressProcessing,
)
from evolvable_memory.application.ports import (
    AuthorizationAuditPort,
    AuthorizationPort,
    Clock,
    MemoryStore,
    PrivacyGovernancePort,
)
from evolvable_memory.application.security import (
    ActorContext,
    AuthenticationError,
    AuthorizationDeniedError,
    InvocationContext,
)
from evolvable_memory.application.service import MemoryApplication
from evolvable_memory.composition import build_recall_projection
from evolvable_memory.config import Settings
from evolvable_memory.domain.common import (
    AttributionError,
    ConflictError,
    ContextSignature,
    DomainError,
    NotFoundError,
    Scope,
)
from evolvable_memory.domain.governance import (
    GovernanceUnavailableError,
    ProcessingDeniedError,
)

_OPENAPI_TAGS = [
    {
        "name": "operations",
        "description": "服务发现与存活检查。",
    },
    {
        "name": "memory",
        "description": "写入偏好、查看当前信念、追加修订并读取不可变历史。",
    },
    {
        "name": "recall",
        "description": (
            "按双时间可见性、相关性、上下文、信念、效用和时效进行可追踪召回, "
            "并从 Trace 生成可归因的有界上下文投影。"
        ),
    },
    {
        "name": "experience",
        "description": "记录引用 RecallTrace 的真实结果, 更新上下文效用。",
    },
    {
        "name": "governance",
        "description": "管理可信处理依据、立即抑制、删除编排和最小删除证明。",
    },
]
_BEARER = HTTPBearer(
    auto_error=False,
    scheme_name="OAuth2AccessToken",
    description=(
        "Production mode requires an RFC 9068-style access token. "
        "Development mode uses an explicit local-only identity adapter."
    ),
)
_BEARER_DEPENDENCY = Security(_BEARER)

_ERROR_DESCRIPTIONS = {
    status.HTTP_400_BAD_REQUEST: "请求违反领域规则。",
    status.HTTP_401_UNAUTHORIZED: "访问令牌缺失或无效。",
    status.HTTP_403_FORBIDDEN: "调用方无权执行该操作或使用该处理目的。",
    status.HTTP_404_NOT_FOUND: "资源不存在, 或其存在性因作用域隔离而被隐藏。",
    status.HTTP_409_CONFLICT: "幂等键、乐观并发条件或当前状态发生冲突。",
    status.HTTP_413_CONTENT_TOO_LARGE: "请求体超过服务端配置的大小限制。",
    status.HTTP_503_SERVICE_UNAVAILABLE: "必要的治理或持久化依赖不可用。",
}


def _error_responses(*codes: int) -> dict[int | str, dict[str, Any]]:
    return {
        code: {
            "model": ErrorResponse,
            "description": _ERROR_DESCRIPTIONS[code],
        }
        for code in codes
    }


_OUTCOME_UNPROCESSABLE_RESPONSE: dict[str, Any] = {
    "description": "请求结构校验失败, 或 revision/摘要无法归因到指定 Trace/Usage。",
    "content": {
        "application/json": {
            "schema": {
                "oneOf": [
                    {"$ref": "#/components/schemas/HTTPValidationError"},
                    {"$ref": "#/components/schemas/ErrorResponse"},
                ]
            }
        }
    },
}


def create_app(
    application: MemoryApplication | None = None,
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    authorization: AuthorizationPort | None = None,
    authorization_audit: AuthorizationAuditPort | None = None,
    identity_resolver: IdentityResolver | None = None,
    privacy_governance: PrivacyGovernancePort | None = None,
) -> FastAPI:
    runtime = settings or Settings.from_environment()
    runtime_clock = clock or SystemClock()
    owns_application = application is None
    service = application or _build_application(runtime, runtime_clock)
    owns_privacy = privacy_governance is None
    governance = privacy_governance or _build_privacy_governance(runtime)
    privacy = PrivacyApplication(
        governance=governance,
        memory=service,
        clock=runtime_clock,
        ids=Uuid4Generator(),
        policy_version=runtime.privacy_policy_version,
    )
    owns_audit = authorization_audit is None
    audit = authorization_audit or _build_authorization_audit(runtime)
    access = AuthorizedMemoryApplication(
        application=service,
        authorization=authorization or RolePolicyAuthorizer(),
        audit=audit,
        clock=runtime_clock,
        privacy=privacy,
    )
    identities = identity_resolver or _build_identity_resolver(runtime)

    def authenticated_actor(
        credentials: HTTPAuthorizationCredentials | None = _BEARER_DEPENDENCY,
    ) -> ActorContext:
        token = credentials.credentials if credentials is not None else None
        return identities.authenticate(token)

    actor_dependency = Depends(authenticated_actor)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        try:
            if owns_audit and hasattr(audit, "close"):
                audit.close()
        finally:
            try:
                if owns_privacy:
                    privacy.close()
            finally:
                if owns_application:
                    service.close()

    app = FastAPI(
        title="Evolvable User Memory",
        version=__version__,
        description=(
            "一套证据驱动、结果感知的上下文记忆服务。\n\n"
            "推荐体验顺序: **写入偏好 → 查看当前记忆 → 召回 → 提交结果 → 修正并查看历史**。\n\n"
            + (
                "当前运行于进程内存模式, 仅用于开发和语义验证; 重启会清空数据。"
                if runtime.store == "memory"
                else (
                    "当前使用 PostgreSQL 权威存储, 是否可接入真实数据由服务发现中的"
                    "生产 blocker、部署审批和外部运维治理共同决定。"
                    + (
                        " Milvus 作为可重建的语义召回投影, 最终可见性仍由 PostgreSQL 判定。"
                        if runtime.projection_mode == "milvus"
                        else ""
                    )
                )
            )
        ),
        openapi_tags=_OPENAPI_TAGS,
        lifespan=lifespan,
    )
    app.state.memory_application = service
    app.state.authorized_memory_application = access
    app.state.privacy_application = privacy
    app.state.settings = runtime
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime.cors_origins),
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(
        ApiRuntimeMiddleware,
        max_request_body_bytes=runtime.max_request_body_bytes,
    )

    @app.exception_handler(RequestBodyTooLargeError)
    async def request_too_large_handler(
        request: Request,
        exc: RequestBodyTooLargeError,
    ) -> JSONResponse:
        body = ErrorResponse(
            error=type(exc).__name__,
            detail=str(exc.detail),
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content=body.model_dump(mode="json"),
        )

    @app.exception_handler(AuthenticationError)
    async def authentication_error_handler(
        request: Request,
        _exc: AuthenticationError,
    ) -> JSONResponse:
        body = ErrorResponse(
            error="AuthenticationError",
            detail="A valid bearer access token is required.",
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Bearer"},
            content=body.model_dump(mode="json"),
        )

    @app.exception_handler(AuthorizationDeniedError)
    async def authorization_error_handler(
        request: Request,
        exc: AuthorizationDeniedError,
    ) -> JSONResponse:
        code = status.HTTP_404_NOT_FOUND if exc.conceal_resource else status.HTTP_403_FORBIDDEN
        body = ErrorResponse(
            error="NotFoundError" if exc.conceal_resource else "AuthorizationDeniedError",
            detail=str(exc),
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=code, content=body.model_dump(mode="json"))

    @app.exception_handler(ProcessingDeniedError)
    async def processing_denied_handler(
        request: Request,
        exc: ProcessingDeniedError,
    ) -> JSONResponse:
        body = ErrorResponse(
            error="ProcessingDeniedError",
            detail=exc.reason,
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content=body.model_dump(mode="json"),
        )

    @app.exception_handler(GovernanceUnavailableError)
    async def governance_unavailable_handler(
        request: Request,
        exc: GovernanceUnavailableError,
    ) -> JSONResponse:
        body = ErrorResponse(
            error="GovernanceUnavailableError",
            detail=str(exc),
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=body.model_dump(mode="json"),
        )

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request: Request, exc: DomainError) -> JSONResponse:
        if isinstance(exc, NotFoundError):
            code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, ConflictError):
            code = status.HTTP_409_CONFLICT
        elif isinstance(exc, AttributionError):
            code = status.HTTP_422_UNPROCESSABLE_CONTENT
        else:
            code = status.HTTP_400_BAD_REQUEST
        body = ErrorResponse(
            error=type(exc).__name__,
            detail=str(exc),
            request_id=getattr(_request.state, "request_id", None),
        )
        return JSONResponse(status_code=code, content=body.model_dump(mode="json"))

    @app.get(
        "/",
        response_model=ServiceInfoResponse,
        tags=["operations"],
        summary="发现服务入口",
        description="返回前端、OpenAPI 文档和当前运行边界, 适合首次访问时确认服务。",
    )
    def service_info() -> ServiceInfoResponse:
        blockers = production_blockers(
            store=runtime.store,
            auth_mode=runtime.auth_mode,
            governance_mode=runtime.governance_mode,
            governance_ready=privacy.is_ready(),
            audit_sink=runtime.auth_audit_sink,
            audit_ready=_dependency_ready(audit),
            environment=runtime.environment,
        )
        return ServiceInfoResponse(
            name="Evolvable User Memory",
            version=__version__,
            api_contract=API_CONTRACT,
            capabilities=API_CAPABILITIES,
            status="ok",
            storage=runtime.store,
            auth_mode=runtime.auth_mode,
            scope_source="request" if runtime.auth_mode == "development" else "access_token",
            frontend_url=runtime.frontend_url,
            documentation_url=f"{runtime.public_api_url.rstrip('/')}/docs",
            production_ready=not blockers,
            production_blockers=blockers,
            notice=(
                "Development contract: data is cleared when the backend restarts."
                if runtime.store == "memory"
                else (
                    "PostgreSQL authority enabled; production readiness is derived from "
                    "trusted JWT, persistent governance, erasure, and audit readiness."
                )
            ),
        )

    @app.get("/health", tags=["operations"], summary="检查服务存活状态")
    def health() -> dict[str, str]:
        return {
            "status": "ok",
            "version": __version__,
            "storage": runtime.store,
            "auth_mode": runtime.auth_mode,
            "scope_source": ("request" if runtime.auth_mode == "development" else "access_token"),
            "projection": service.projection_status,
        }

    @app.get("/livez", tags=["operations"], summary="检查进程存活状态")
    def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/readyz",
        response_model=ReadinessResponse,
        responses={
            status.HTTP_503_SERVICE_UNAVAILABLE: {
                "model": ReadinessResponse,
                "description": "权威存储或其他必要依赖尚未就绪。",
            }
        },
        tags=["operations"],
        summary="检查依赖就绪状态",
    )
    def readyz(response: Response) -> ReadinessResponse:
        if service.is_ready() and privacy.is_ready() and _dependency_ready(audit):
            return ReadinessResponse(status="ready", storage=runtime.store)
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="not_ready", storage=runtime.store)

    @app.post(
        "/v1/preferences",
        response_model=PreferenceResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["memory"],
        summary="记录一条上下文偏好",
        description=(
            "保存不可变 Observation 与 EvidenceSpan, 并创建或追加偏好修订。"
            "相同作用域和幂等键的安全重试不会重复写入。"
        ),
        responses=_error_responses(400, 401, 403, 404, 409, 413),
    )
    def remember_preference(
        payload: PreferenceWriteRequest,
        http_request: Request,
        actor: ActorContext = actor_dependency,
    ) -> PreferenceResponse:
        scope = Scope(payload.tenant_id, payload.subject_id)
        result = access.remember_preference(
            _invocation(http_request, actor, payload.purpose),
            RememberPreference(
                scope=scope,
                source=payload.source,
                idempotency_key=payload.idempotency_key,
                key=payload.key,
                value=payload.value,
                context=ContextSignature.from_mapping(payload.context),
                evidence_text=payload.evidence_text,
                confidence=payload.confidence,
                occurred_at=_occurred_at(payload.occurred_at, runtime_clock),
            ),
        )
        return PreferenceResponse.from_result(result)

    @app.post(
        "/v1/preferences/{record_id}/corrections",
        response_model=PreferenceResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["memory"],
        summary="修正一条偏好",
        description="追加新修订并保留旧版本; 不会原地覆盖历史。",
        responses=_error_responses(400, 401, 403, 404, 409, 413),
    )
    def correct_preference(
        record_id: UUID,
        payload: PreferenceCorrectionRequest,
        http_request: Request,
        actor: ActorContext = actor_dependency,
    ) -> PreferenceResponse:
        scope = Scope(payload.tenant_id, payload.subject_id)
        result = access.correct_preference(
            _invocation(http_request, actor, payload.purpose),
            CorrectPreference(
                scope=scope,
                record_id=record_id,
                source=payload.source,
                idempotency_key=payload.idempotency_key,
                value=payload.value,
                evidence_text=payload.evidence_text,
                reason=payload.reason,
                expected_revision_id=payload.expected_revision_id,
                occurred_at=_occurred_at(payload.occurred_at, runtime_clock),
            ),
        )
        return PreferenceResponse.from_result(result)

    @app.get(
        "/v1/preferences",
        response_model=list[PreferenceSummaryResponse],
        tags=["memory"],
        summary="列出当前有效偏好",
        description="只返回指定租户和用户作用域内每条偏好的当前有效修订。",
        responses=_error_responses(401, 403, 404),
    )
    def list_preferences(
        http_request: Request,
        tenant_id: ScopeId,
        subject_id: ScopeId,
        actor: ActorContext = actor_dependency,
        purpose: PurposeText = "personalization",
    ) -> list[PreferenceSummaryResponse]:
        scope = Scope(tenant_id, subject_id)
        snapshots = access.list_preferences(
            _invocation(http_request, actor, purpose),
            scope,
        )
        return [PreferenceSummaryResponse.from_snapshot(snapshot) for snapshot in snapshots]

    @app.get(
        "/v1/preferences/{record_id}/revisions",
        response_model=list[RevisionResponse],
        tags=["memory"],
        summary="读取偏好修订历史",
        description="按修订序号返回完整的不可变版本链。",
        responses=_error_responses(401, 403, 404),
    )
    def preference_history(
        record_id: UUID,
        http_request: Request,
        tenant_id: ScopeId,
        subject_id: ScopeId,
        actor: ActorContext = actor_dependency,
        purpose: PurposeText = "personalization",
    ) -> list[RevisionResponse]:
        scope = Scope(tenant_id, subject_id)
        revisions = access.history(
            _invocation(http_request, actor, purpose),
            scope,
            record_id,
        )
        return [RevisionResponse.from_revision(revision) for revision in revisions]

    @app.post(
        "/v1/recall",
        response_model=RecallResponse,
        tags=["recall"],
        summary="执行上下文记忆召回",
        description=(
            "返回带双时间边界、评分拆解的结果和 trace_id。valid_at 控制业务有效时点。"
            "known_at 控制系统知识截止时点。两者缺省为同一次服务端当前时间。"
            "召回使用当前不可变策略快照。本身不会修改信念或效用。"
            "业务结果应通过 /v1/outcomes 回传。"
        ),
        responses=_error_responses(400, 401, 403, 404, 413),
    )
    def recall(
        payload: RecallRequest,
        http_request: Request,
        actor: ActorContext = actor_dependency,
    ) -> RecallResponse:
        scope = Scope(payload.tenant_id, payload.subject_id)
        trace = access.recall(
            _invocation(http_request, actor, payload.purpose),
            RecallMemory(
                scope=scope,
                query=payload.query,
                context=ContextSignature.from_mapping(payload.context),
                limit=payload.limit,
                valid_at=payload.valid_at,
                known_at=payload.known_at,
            ),
        )
        return RecallResponse.from_trace(trace)

    @app.post(
        "/v1/recall-contexts",
        response_model=RecallContextProjectionResponse,
        tags=["recall"],
        summary="压缩一次召回的可归因上下文",
        description=(
            "从不可变 RecallTrace 生成字符预算内的确定性 JSON 投影。"
            "每个片段保留源 record/revision、排名和得分, 不会改写证据、创建信念或学习效用。"
            "相同 Trace、算法和预算会得到相同内容与 SHA-256。"
        ),
        responses=_error_responses(400, 401, 403, 404, 413),
    )
    def project_recall_context(
        payload: RecallContextProjectionRequest,
        http_request: Request,
        actor: ActorContext = actor_dependency,
    ) -> RecallContextProjectionResponse:
        projection = access.project_recall_context(
            _invocation(http_request, actor, payload.purpose),
            ProjectRecallContext(
                scope=Scope(payload.tenant_id, payload.subject_id),
                trace_id=payload.trace_id,
                algorithm=payload.algorithm,
                budget_characters=payload.max_characters,
            ),
        )
        return RecallContextProjectionResponse.from_projection(projection)

    @app.post(
        "/v1/usages",
        response_model=MemoryUsageResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["experience"],
        summary="记录实际进入消费者上下文的记忆",
        description=(
            "服务端从不可变 Trace 重建投影并核对摘要, 只接受该投影中的 revision。"
            "Outcome 可以引用返回的 usage_id, 避免把仅召回但未实际使用的记忆算作效果。"
        ),
        responses={
            **_error_responses(400, 401, 403, 404, 409, 413),
            status.HTTP_422_UNPROCESSABLE_CONTENT: _OUTCOME_UNPROCESSABLE_RESPONSE,
        },
    )
    def record_memory_usage(
        payload: MemoryUsageWriteRequest,
        http_request: Request,
        actor: ActorContext = actor_dependency,
    ) -> MemoryUsageResponse:
        result = access.record_usage(
            _invocation(http_request, actor, payload.purpose),
            RecordMemoryUsage(
                scope=Scope(payload.tenant_id, payload.subject_id),
                trace_id=payload.trace_id,
                algorithm=payload.algorithm,
                budget_characters=payload.max_characters,
                source_projection_sha256=payload.source_projection_sha256,
                delivered_context_sha256=payload.delivered_context_sha256,
                revision_ids=tuple(payload.revision_ids),
                idempotency_key=payload.idempotency_key,
                occurred_at=_occurred_at(payload.occurred_at, runtime_clock),
            ),
        )
        return MemoryUsageResponse.from_result(result)

    @app.post(
        "/v1/outcomes",
        response_model=OutcomeResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["experience"],
        summary="记录可归因的业务结果",
        description=(
            "Outcome 必须引用一次召回的 trace_id。提供 usage_id 时, revision 必须存在于该次"
            "实际使用; 兼容旧调用方的无 usage 请求仍按 Trace 校验。"
            "这是一条记忆学习上下文效用的唯一入口。"
        ),
        responses={
            **_error_responses(400, 401, 403, 404, 409, 413),
            status.HTTP_422_UNPROCESSABLE_CONTENT: _OUTCOME_UNPROCESSABLE_RESPONSE,
        },
    )
    def record_outcome(
        payload: OutcomeWriteRequest,
        http_request: Request,
        actor: ActorContext = actor_dependency,
    ) -> OutcomeResponse:
        scope = Scope(payload.tenant_id, payload.subject_id)
        result = access.record_outcome(
            _invocation(http_request, actor, payload.purpose),
            RecordOutcome(
                scope=scope,
                trace_id=payload.trace_id,
                revision_id=payload.revision_id,
                usage_id=payload.usage_id,
                kind=payload.kind,
                idempotency_key=payload.idempotency_key,
                occurred_at=_occurred_at(payload.occurred_at, runtime_clock),
                weight=payload.weight,
                note=payload.note,
            ),
        )
        return OutcomeResponse.from_result(result)

    @app.post(
        "/v1/governance/processing-grants",
        response_model=ProcessingGrantResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["governance"],
        summary="签发受用途约束的处理依据",
        responses=_error_responses(400, 401, 403, 404, 409, 413, 503),
    )
    def issue_processing_grant(
        payload: ProcessingGrantWriteRequest,
        http_request: Request,
        actor: ActorContext = actor_dependency,
    ) -> ProcessingGrantResponse:
        grant = access.issue_processing_grant(
            _invocation(http_request, actor, payload.purpose),
            IssueProcessingGrant(
                scope=Scope(payload.tenant_id, payload.subject_id),
                purposes=tuple(payload.purposes),
                lawful_basis=payload.lawful_basis,
                idempotency_key=payload.idempotency_key,
                valid_from=payload.valid_from,
                valid_until=payload.valid_until,
            ),
        )
        return ProcessingGrantResponse.from_grant(grant)

    @app.post(
        "/v1/governance/processing-grants/{grant_id}/revocation",
        response_model=ProcessingGrantResponse,
        tags=["governance"],
        summary="撤销处理依据",
        responses=_error_responses(400, 401, 403, 404, 503),
    )
    def revoke_processing_grant(
        grant_id: UUID,
        payload: ProcessingGrantRevocationRequest,
        http_request: Request,
        actor: ActorContext = actor_dependency,
    ) -> ProcessingGrantResponse:
        grant = access.revoke_processing_grant(
            _invocation(http_request, actor, payload.purpose),
            RevokeProcessingGrant(
                scope=Scope(payload.tenant_id, payload.subject_id),
                grant_id=grant_id,
            ),
        )
        return ProcessingGrantResponse.from_grant(grant)

    @app.post(
        "/v1/governance/suppressions",
        response_model=SuppressionResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["governance"],
        summary="立即停止一个 Scope 的全部普通处理",
        responses=_error_responses(400, 401, 403, 404, 409, 413, 503),
    )
    def suppress_processing(
        payload: SuppressionRequest,
        http_request: Request,
        actor: ActorContext = actor_dependency,
    ) -> SuppressionResponse:
        fence = access.suppress(
            _invocation(http_request, actor, payload.purpose),
            SuppressProcessing(
                scope=Scope(payload.tenant_id, payload.subject_id),
                reason_code=payload.reason_code,
                idempotency_key=payload.idempotency_key,
            ),
        )
        return SuppressionResponse.from_fence(fence)

    @app.post(
        "/v1/governance/erasures",
        response_model=ErasureResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["governance"],
        summary="建立抑制屏障并编排完整 Scope 删除",
        responses=_error_responses(400, 401, 403, 404, 409, 413, 503),
    )
    def erase_subject(
        payload: ErasureWriteRequest,
        http_request: Request,
        actor: ActorContext = actor_dependency,
    ) -> ErasureResponse:
        request = access.erase(
            _invocation(http_request, actor, payload.purpose),
            EraseSubject(
                scope=Scope(payload.tenant_id, payload.subject_id),
                reason_code=payload.reason_code,
                idempotency_key=payload.idempotency_key,
            ),
        )
        return ErasureResponse.from_request(request)

    @app.get(
        "/v1/governance/erasures/{request_id}",
        response_model=ErasureResponse,
        tags=["governance"],
        summary="读取最小删除证明",
        responses=_error_responses(401, 403, 404, 503),
    )
    def erasure_receipt(
        request_id: UUID,
        http_request: Request,
        tenant_id: ScopeId,
        subject_id: ScopeId,
        actor: ActorContext = actor_dependency,
        purpose: PurposeText = "privacy-governance",
    ) -> ErasureResponse:
        request = access.erasure(
            _invocation(http_request, actor, purpose),
            Scope(tenant_id, subject_id),
            request_id,
        )
        return ErasureResponse.from_request(request)

    return app


def _build_application(settings: Settings, clock: Clock) -> MemoryApplication:
    store: MemoryStore
    if settings.store == "postgres":
        if settings.database_url is None:
            raise RuntimeError("database_url was not validated")
        store = PostgresMemoryStore(
            settings.database_url,
            min_size=settings.database_pool_min_size,
            max_size=settings.database_pool_max_size,
            readiness_timeout=settings.database_readiness_timeout_seconds,
        )
    else:
        store = InMemoryMemoryStore()
    return MemoryApplication(
        store=store,
        clock=clock,
        ids=Uuid4Generator(),
        recall_projection=build_recall_projection(settings),
        projection_required=settings.projection_required,
        projection_search_oversample=settings.projection_search_oversample,
    )


def _build_privacy_governance(settings: Settings) -> PrivacyGovernancePort:
    if settings.governance_mode == "development":
        return DevelopmentBypassPrivacyGovernance()
    if settings.database_url is None or settings.governance_hmac_key is None:
        raise RuntimeError("persistent governance settings were not validated")
    return PostgresPrivacyGovernance(
        settings.database_url,
        hmac_key=settings.governance_hmac_key.encode(),
        pseudonym_key_id=settings.governance_pseudonym_key_id,
        min_size=settings.database_pool_min_size,
        max_size=settings.database_pool_max_size,
        readiness_timeout=settings.database_readiness_timeout_seconds,
    )


def _build_authorization_audit(settings: Settings) -> AuthorizationAuditPort:
    key = (settings.auth_audit_hmac_key or "development-only-audit-key-change-me").encode()
    if settings.auth_audit_sink == "log":
        return LoggingAuthorizationAuditSink(key)
    if settings.database_url is None:
        raise RuntimeError("persistent audit settings were not validated")
    return PostgresAuthorizationAuditSink(
        settings.database_url,
        hmac_key=key,
        pseudonym_key_id=settings.governance_pseudonym_key_id,
        min_size=settings.database_pool_min_size,
        max_size=settings.database_pool_max_size,
        readiness_timeout=settings.database_readiness_timeout_seconds,
    )


def _dependency_ready(dependency: object) -> bool:
    readiness = getattr(dependency, "is_ready", None)
    return bool(readiness()) if callable(readiness) else True


def _build_identity_resolver(settings: Settings) -> IdentityResolver:
    if settings.auth_mode == "development":
        return DevelopmentIdentityResolver()
    issuer = settings.auth_jwt_issuer
    audience = settings.auth_jwt_audience
    jwks_url = settings.auth_jwt_jwks_url
    if issuer is None or audience is None or jwks_url is None:
        raise RuntimeError("JWT identity settings were not validated")
    return JwtIdentityResolver(
        issuer=issuer,
        audience=audience,
        algorithms=settings.auth_jwt_algorithms,
        required_scope=settings.auth_required_scope,
        key_provider=PyJwkSigningKeyProvider(jwks_url),
    )


def _invocation(
    request: Request,
    actor: ActorContext,
    purpose: str,
) -> InvocationContext:
    request_id = getattr(request.state, "request_id", None)
    if not isinstance(request_id, str):
        raise RuntimeError("request identity middleware did not establish a request ID")
    return InvocationContext(actor=actor, purpose=purpose, request_id=request_id)


def _occurred_at(value: datetime | None, clock: Clock) -> datetime:
    return value if value is not None else clock.now()


class _LazyCompatibilityApplication:
    """Preserve ``api.app:app`` without composing infrastructure at import time."""

    def __init__(self, factory: Callable[[], FastAPI]) -> None:
        self._factory = factory
        self._application: FastAPI | None = None
        self._lock = Lock()

    def _load(self) -> FastAPI:
        application = self._application
        if application is not None:
            return application
        with self._lock:
            application = self._application
            if application is None:
                application = self._factory()
                self._application = application
        return application

    async def __call__(
        self,
        scope: AsgiScope,
        receive: Receive,
        send: Send,
    ) -> None:
        await self._load()(scope, receive, send)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._load(), name)


# Compatibility for deployments that still use ``evolvable_memory.api.app:app``.
# The actual service is composed on the first ASGI use, never during module import.
app: ASGIApp = _LazyCompatibilityApplication(create_app)
