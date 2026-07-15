from __future__ import annotations

import os
from dataclasses import dataclass

from evolvable_memory.domain.common import DomainError


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
    max_request_body_bytes: int = 1_048_576
    auth_mode: str = "development"
    auth_jwt_issuer: str | None = None
    auth_jwt_audience: str | None = None
    auth_jwt_jwks_url: str | None = None
    auth_jwt_algorithms: tuple[str, ...] = ("RS256",)
    auth_required_scope: str = "memory"
    auth_audit_hmac_key: str | None = None
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
        if not self.cors_origins:
            raise DomainError("at least one CORS origin must be configured")

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

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65_535:
            raise DomainError("frontend port must be in [1, 65535]")

    @classmethod
    def from_environment(cls) -> FrontendSettings:
        return cls(
            host=os.getenv("EMF_FRONTEND_HOST", "127.0.0.1"),
            port=int(os.getenv("EMF_FRONTEND_PORT", "33009")),
        )
