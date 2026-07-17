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
    RolePolicyAuthorizer,
)
from evolvable_memory.adapters.in_memory import InMemoryMemoryStore
from evolvable_memory.adapters.postgres import PostgresMemoryStore
from evolvable_memory.adapters.system import SystemClock, Uuid4Generator
from evolvable_memory.api.middleware import ApiRuntimeMiddleware, RequestBodyTooLargeError
from evolvable_memory.api.schemas import (
    ErrorResponse,
    OutcomeResponse,
    OutcomeWriteRequest,
    PreferenceCorrectionRequest,
    PreferenceResponse,
    PreferenceSummaryResponse,
    PreferenceWriteRequest,
    PurposeText,
    ReadinessResponse,
    RecallRequest,
    RecallResponse,
    RevisionResponse,
    ScopeId,
    ServiceInfoResponse,
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
    RecallMemory,
    RecordOutcome,
    RememberPreference,
)
from evolvable_memory.application.ports import (
    AuthorizationAuditPort,
    AuthorizationPort,
    Clock,
    MemoryStore,
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
        "description": "按双时间可见性、相关性、上下文、信念、效用和时效进行可追踪召回。",
    },
    {
        "name": "experience",
        "description": "记录引用 RecallTrace 的真实结果, 更新上下文效用。",
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
    "description": "请求结构校验失败, 或 revision 无法归因到指定 RecallTrace。",
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
) -> FastAPI:
    runtime = settings or Settings.from_environment()
    runtime_clock = clock or SystemClock()
    owns_application = application is None
    service = application or _build_application(runtime, runtime_clock)
    access = AuthorizedMemoryApplication(
        application=service,
        authorization=authorization or RolePolicyAuthorizer(),
        audit=authorization_audit
        or LoggingAuthorizationAuditSink(
            (runtime.auth_audit_hmac_key or "development-only-audit-key-change-me").encode()
        ),
        clock=runtime_clock,
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
                    "当前使用 PostgreSQL 权威存储; 仍需完整权限治理、隐私治理"
                    "和生产运维后才能接入真实数据。"
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
        return ServiceInfoResponse(
            name="Evolvable User Memory",
            version=__version__,
            status="ok",
            storage=runtime.store,
            auth_mode=runtime.auth_mode,
            scope_source="request" if runtime.auth_mode == "development" else "access_token",
            frontend_url=runtime.frontend_url,
            documentation_url=f"{runtime.public_api_url.rstrip('/')}/docs",
            production_ready=False,
            notice=(
                "Development contract: data is cleared when the backend restarts."
                if runtime.store == "memory"
                else (
                    "PostgreSQL authority and authorization baseline enabled; permission "
                    "governance and privacy policy remain required."
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
        if service.is_ready():
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
        "/v1/outcomes",
        response_model=OutcomeResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["experience"],
        summary="记录可归因的业务结果",
        description=(
            "Outcome 必须引用一次召回的 trace_id, 且 revision_id 必须存在于该 Trace。"
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
                kind=payload.kind,
                idempotency_key=payload.idempotency_key,
                occurred_at=_occurred_at(payload.occurred_at, runtime_clock),
                weight=payload.weight,
                note=payload.note,
            ),
        )
        return OutcomeResponse.from_result(result)

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
