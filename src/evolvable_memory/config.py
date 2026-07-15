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
    frontend_url: str = "http://127.0.0.1:33009"
    public_api_url: str = "http://127.0.0.1:38089"
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:33009",
        "http://localhost:33009",
    )

    def __post_init__(self) -> None:
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
        if not self.cors_origins:
            raise DomainError("at least one CORS origin must be configured")

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            environment=os.getenv("EMF_ENVIRONMENT", "development"),
            host=os.getenv("EMF_HOST", "127.0.0.1"),
            port=int(os.getenv("EMF_PORT", "38089")),
            log_level=os.getenv("EMF_LOG_LEVEL", "INFO").upper(),
            store=os.getenv("EMF_STORE", "memory"),
            database_url=os.getenv("EMF_DATABASE_URL"),
            database_pool_min_size=int(os.getenv("EMF_DATABASE_POOL_MIN_SIZE", "1")),
            database_pool_max_size=int(os.getenv("EMF_DATABASE_POOL_MAX_SIZE", "10")),
            max_request_body_bytes=int(os.getenv("EMF_MAX_REQUEST_BODY_BYTES", "1048576")),
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
