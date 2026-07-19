from __future__ import annotations

import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from evolvable_memory.domain.common import DomainError

_MILVUS_COLLECTION_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")


def _environment_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise DomainError(f"{name} must be a boolean")


def _validate_http_url(value: str, name: str, *, origin_only: bool = False) -> None:
    if (
        not value
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise DomainError(f"{name} must be a valid HTTP(S) URL")
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
        parsed.netloc.encode("ascii")
    except (UnicodeEncodeError, ValueError) as exc:
        raise DomainError(f"{name} must be a valid HTTP(S) URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (parsed_port is not None and not 1 <= parsed_port <= 65_535)
    ):
        raise DomainError(f"{name} must be a valid HTTP(S) URL")
    if origin_only and parsed.path:
        raise DomainError(f"{name} must contain origins without paths")


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 38089
    log_level: str = "INFO"
    store: str = "memory"
    database_url: str | None = None
    database_pool_min_size: int = 1
    database_pool_max_size: int = 10
    database_readiness_timeout_seconds: float = 1.0
    projection_mode: str = "disabled"
    projection_required: bool = False
    projection_name: str = "milvus-memory-v1"
    projection_search_oversample: int = 10
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: str | None = None
    milvus_collection: str = "evolvable_memory_v1"
    milvus_consistency_level: str = "Bounded"
    milvus_timeout_seconds: float = 5.0
    milvus_min_similarity: float = 0.15
    embedding_provider: str = "hash"
    embedding_model: str = "hash-blake2b-v1"
    embedding_dimensions: int = 384
    embedding_base_url: str = "http://127.0.0.1:11434/v1"
    embedding_api_key: str | None = None
    embedding_timeout_seconds: float = 15.0
    projection_worker_batch_size: int = 64
    projection_worker_lease_seconds: float = 60.0
    projection_worker_poll_seconds: float = 1.0
    projection_worker_retry_base_seconds: float = 2.0
    projection_worker_retry_max_seconds: float = 300.0
    projection_worker_max_attempts: int = 8
    max_request_body_bytes: int = 1_048_576
    auth_mode: str = "development"
    auth_jwt_issuer: str | None = None
    auth_jwt_audience: str | None = None
    auth_jwt_jwks_url: str | None = None
    auth_jwt_algorithms: tuple[str, ...] = ("RS256",)
    auth_required_scope: str = "memory"
    auth_audit_hmac_key: str | None = None
    auth_audit_sink: str = "log"
    governance_mode: str = "development"
    governance_hmac_key: str | None = None
    governance_pseudonym_key_id: str = "governance-v1"
    privacy_policy_version: str = "privacy-v1"
    frontend_url: str = "http://127.0.0.1:33009"
    public_api_url: str = "http://127.0.0.1:38089"
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:33009",
        "http://localhost:33009",
    )

    def __post_init__(self) -> None:
        if self.environment not in {"development", "test", "staging", "production"}:
            raise DomainError("environment must be development, test, staging, or production")
        if not 1 <= self.port <= 65_535:
            raise DomainError("port must be in [1, 65535]")
        if self.store not in {"memory", "postgres"}:
            raise DomainError("store must be either 'memory' or 'postgres'")
        if self.store == "postgres" and not self.database_url:
            raise DomainError("EMF_DATABASE_URL is required when EMF_STORE=postgres")
        if self.database_pool_min_size < 1:
            raise DomainError("database_pool_min_size must be positive")
        if self.database_pool_max_size < self.database_pool_min_size:
            raise DomainError("database_pool_max_size must be >= database_pool_min_size")
        if not 0.05 <= self.database_readiness_timeout_seconds <= 30:
            raise DomainError("database_readiness_timeout_seconds must be in [0.05, 30]")
        if self.projection_mode not in {"disabled", "milvus"}:
            raise DomainError("projection_mode must be either 'disabled' or 'milvus'")
        if self.projection_mode == "milvus" and self.store != "postgres":
            raise DomainError("Milvus projection requires EMF_STORE=postgres")
        if self.projection_required and self.projection_mode == "disabled":
            raise DomainError("a disabled projection cannot be required")
        if not self.projection_name.strip():
            raise DomainError("projection_name must not be blank")
        if not 1 <= self.projection_search_oversample <= 100:
            raise DomainError("projection_search_oversample must be in [1, 100]")
        _validate_http_url(self.milvus_uri, "milvus_uri")
        if not _MILVUS_COLLECTION_PATTERN.fullmatch(self.milvus_collection):
            raise DomainError("milvus_collection must be a valid Milvus collection name")
        if self.milvus_consistency_level not in {"Strong", "Bounded", "Eventually", "Session"}:
            raise DomainError("milvus_consistency_level is unsupported")
        if not 0.05 <= self.milvus_timeout_seconds <= 120:
            raise DomainError("milvus_timeout_seconds must be in [0.05, 120]")
        if not 0.0 <= self.milvus_min_similarity <= 1.0:
            raise DomainError("milvus_min_similarity must be between 0 and 1")
        if self.embedding_provider not in {"hash", "openai_compatible"}:
            raise DomainError("embedding_provider must be 'hash' or 'openai_compatible'")
        if not self.embedding_model.strip():
            raise DomainError("embedding_model must not be blank")
        if not 32 <= self.embedding_dimensions <= 32_768:
            raise DomainError("embedding_dimensions must be in [32, 32768]")
        _validate_http_url(self.embedding_base_url, "embedding_base_url")
        if not 0.05 <= self.embedding_timeout_seconds <= 300:
            raise DomainError("embedding_timeout_seconds must be in [0.05, 300]")
        if not 1 <= self.projection_worker_batch_size <= 1_000:
            raise DomainError("projection_worker_batch_size must be in [1, 1000]")
        if not 1 <= self.projection_worker_lease_seconds <= 3_600:
            raise DomainError("projection_worker_lease_seconds must be in [1, 3600]")
        if not 0.05 <= self.projection_worker_poll_seconds <= 60:
            raise DomainError("projection_worker_poll_seconds must be in [0.05, 60]")
        if not 0.05 <= self.projection_worker_retry_base_seconds <= 3_600:
            raise DomainError("projection_worker_retry_base_seconds must be in [0.05, 3600]")
        if not (
            self.projection_worker_retry_base_seconds
            <= self.projection_worker_retry_max_seconds
            <= 86_400
        ):
            raise DomainError("projection retry max must be >= base and <= 86400")
        if not 1 <= self.projection_worker_max_attempts <= 100:
            raise DomainError("projection_worker_max_attempts must be in [1, 100]")
        if self.max_request_body_bytes < 1:
            raise DomainError("max_request_body_bytes must be positive")
        if self.auth_mode not in {"development", "jwt"}:
            raise DomainError("auth_mode must be either 'development' or 'jwt'")
        if self.environment not in {"development", "test"} and self.auth_mode == "development":
            raise DomainError("development authentication is forbidden outside development/test")
        if self.auth_mode == "jwt":
            issuer = self.auth_jwt_issuer
            audience = self.auth_jwt_audience
            jwks_url = self.auth_jwt_jwks_url
            if issuer is None or audience is None or jwks_url is None:
                raise DomainError("JWT authentication requires issuer, audience, and JWKS URL")
            _validate_http_url(issuer, "auth_jwt_issuer")
            _validate_http_url(jwks_url, "auth_jwt_jwks_url")
            if not audience.strip() or audience != audience.strip():
                raise DomainError("JWT authentication requires a valid audience")
            allowed_algorithms = {
                "RS256",
                "RS384",
                "RS512",
                "ES256",
                "ES384",
                "ES512",
                "EdDSA",
            }
            if not self.auth_jwt_algorithms or not set(self.auth_jwt_algorithms).issubset(
                allowed_algorithms
            ):
                raise DomainError("JWT authentication requires asymmetric algorithms")
            if self.auth_audit_hmac_key is None or len(self.auth_audit_hmac_key) < 32:
                raise DomainError("JWT authentication requires a 32-character audit HMAC key")
            if self.environment in {"staging", "production"}:
                if not issuer.startswith("https://"):
                    raise DomainError("production JWT issuer must use HTTPS")
                if not jwks_url.startswith("https://"):
                    raise DomainError("production JWT JWKS URL must use HTTPS")
        if not self.auth_required_scope.strip():
            raise DomainError("auth_required_scope must not be blank")
        if self.auth_audit_sink not in {"log", "postgres"}:
            raise DomainError("auth_audit_sink must be either 'log' or 'postgres'")
        if self.auth_audit_sink == "postgres":
            if self.store != "postgres":
                raise DomainError("persistent authorization audit requires EMF_STORE=postgres")
            if self.auth_audit_hmac_key is None or len(self.auth_audit_hmac_key) < 32:
                raise DomainError("persistent authorization audit requires a 32-character HMAC key")
        if self.governance_mode not in {"development", "postgres"}:
            raise DomainError("governance_mode must be either 'development' or 'postgres'")
        if self.governance_mode == "postgres":
            if self.store != "postgres":
                raise DomainError("persistent privacy governance requires EMF_STORE=postgres")
            if self.governance_hmac_key is None or len(self.governance_hmac_key) < 32:
                raise DomainError("persistent privacy governance requires a 32-character HMAC key")
        if not self.governance_pseudonym_key_id.strip():
            raise DomainError("governance_pseudonym_key_id must not be blank")
        if not self.privacy_policy_version.strip():
            raise DomainError("privacy_policy_version must not be blank")
        if self.environment in {"staging", "production"}:
            if self.auth_audit_sink != "postgres":
                raise DomainError("persistent authorization audit is required in production")
            if self.governance_mode != "postgres":
                raise DomainError("persistent privacy governance is required in production")
        _validate_http_url(self.frontend_url, "frontend_url")
        _validate_http_url(self.public_api_url, "public_api_url")
        if not self.cors_origins:
            raise DomainError("at least one CORS origin must be configured")
        for origin in self.cors_origins:
            _validate_http_url(origin, "cors_origins", origin_only=True)

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            environment=os.getenv("EMF_ENVIRONMENT", "development").lower(),
            host=os.getenv("EMF_HOST", "127.0.0.1"),
            port=int(os.getenv("EMF_PORT", "38089")),
            log_level=os.getenv("EMF_LOG_LEVEL", "INFO").upper(),
            store=os.getenv("EMF_STORE", "memory"),
            database_url=os.getenv("EMF_DATABASE_URL"),
            database_pool_min_size=int(os.getenv("EMF_DATABASE_POOL_MIN_SIZE", "1")),
            database_pool_max_size=int(os.getenv("EMF_DATABASE_POOL_MAX_SIZE", "10")),
            database_readiness_timeout_seconds=float(
                os.getenv("EMF_DATABASE_READINESS_TIMEOUT_SECONDS", "1.0")
            ),
            projection_mode=os.getenv("EMF_PROJECTION_MODE", "disabled").lower(),
            projection_required=_environment_bool("EMF_PROJECTION_REQUIRED"),
            projection_name=os.getenv("EMF_PROJECTION_NAME", "milvus-memory-v1"),
            projection_search_oversample=int(os.getenv("EMF_PROJECTION_SEARCH_OVERSAMPLE", "10")),
            milvus_uri=os.getenv("EMF_MILVUS_URI", "http://127.0.0.1:19530"),
            milvus_token=os.getenv("EMF_MILVUS_TOKEN"),
            milvus_collection=os.getenv("EMF_MILVUS_COLLECTION", "evolvable_memory_v1"),
            milvus_consistency_level=os.getenv("EMF_MILVUS_CONSISTENCY_LEVEL", "Bounded"),
            milvus_timeout_seconds=float(os.getenv("EMF_MILVUS_TIMEOUT_SECONDS", "5.0")),
            milvus_min_similarity=float(os.getenv("EMF_MILVUS_MIN_SIMILARITY", "0.15")),
            embedding_provider=os.getenv("EMF_EMBEDDING_PROVIDER", "hash").lower(),
            embedding_model=os.getenv("EMF_EMBEDDING_MODEL", "hash-blake2b-v1"),
            embedding_dimensions=int(os.getenv("EMF_EMBEDDING_DIMENSIONS", "384")),
            embedding_base_url=os.getenv("EMF_EMBEDDING_BASE_URL", "http://127.0.0.1:11434/v1"),
            embedding_api_key=os.getenv("EMF_EMBEDDING_API_KEY"),
            embedding_timeout_seconds=float(os.getenv("EMF_EMBEDDING_TIMEOUT_SECONDS", "15.0")),
            projection_worker_batch_size=int(os.getenv("EMF_PROJECTION_WORKER_BATCH_SIZE", "64")),
            projection_worker_lease_seconds=float(
                os.getenv("EMF_PROJECTION_WORKER_LEASE_SECONDS", "60.0")
            ),
            projection_worker_poll_seconds=float(
                os.getenv("EMF_PROJECTION_WORKER_POLL_SECONDS", "1.0")
            ),
            projection_worker_retry_base_seconds=float(
                os.getenv("EMF_PROJECTION_WORKER_RETRY_BASE_SECONDS", "2.0")
            ),
            projection_worker_retry_max_seconds=float(
                os.getenv("EMF_PROJECTION_WORKER_RETRY_MAX_SECONDS", "300.0")
            ),
            projection_worker_max_attempts=int(
                os.getenv("EMF_PROJECTION_WORKER_MAX_ATTEMPTS", "8")
            ),
            max_request_body_bytes=int(os.getenv("EMF_MAX_REQUEST_BODY_BYTES", "1048576")),
            auth_mode=os.getenv("EMF_AUTH_MODE", "development").lower(),
            auth_jwt_issuer=os.getenv("EMF_AUTH_JWT_ISSUER"),
            auth_jwt_audience=os.getenv("EMF_AUTH_JWT_AUDIENCE"),
            auth_jwt_jwks_url=os.getenv("EMF_AUTH_JWT_JWKS_URL"),
            auth_jwt_algorithms=tuple(
                algorithm.strip()
                for algorithm in os.getenv("EMF_AUTH_JWT_ALGORITHMS", "RS256").split(",")
                if algorithm.strip()
            ),
            auth_required_scope=os.getenv("EMF_AUTH_REQUIRED_SCOPE", "memory"),
            auth_audit_hmac_key=os.getenv("EMF_AUTH_AUDIT_HMAC_KEY"),
            auth_audit_sink=os.getenv("EMF_AUTH_AUDIT_SINK", "log").lower(),
            governance_mode=os.getenv("EMF_GOVERNANCE_MODE", "development").lower(),
            governance_hmac_key=os.getenv("EMF_GOVERNANCE_HMAC_KEY"),
            governance_pseudonym_key_id=os.getenv(
                "EMF_GOVERNANCE_PSEUDONYM_KEY_ID", "governance-v1"
            ),
            privacy_policy_version=os.getenv("EMF_PRIVACY_POLICY_VERSION", "privacy-v1"),
            frontend_url=os.getenv("EMF_FRONTEND_URL", "http://127.0.0.1:33009"),
            public_api_url=os.getenv("EMF_PUBLIC_API_URL", "http://127.0.0.1:38089"),
            cors_origins=tuple(
                origin.strip()
                for origin in os.getenv(
                    "EMF_CORS_ORIGINS",
                    "http://127.0.0.1:33009,http://localhost:33009",
                ).split(",")
                if origin.strip()
            ),
        )


@dataclass(frozen=True, slots=True)
class FrontendSettings:
    host: str = "127.0.0.1"
    port: int = 33009
    public_api_url: str = "http://127.0.0.1:38089"

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65_535:
            raise DomainError("frontend port must be in [1, 65535]")
        _validate_http_url(self.public_api_url, "public_api_url")

    @classmethod
    def from_environment(cls) -> FrontendSettings:
        return cls(
            host=os.getenv("EMF_FRONTEND_HOST", "127.0.0.1"),
            port=int(os.getenv("EMF_FRONTEND_PORT", "33009")),
            public_api_url=os.getenv("EMF_PUBLIC_API_URL", "http://127.0.0.1:38089"),
        )
