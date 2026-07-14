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

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65_535:
            raise DomainError("port must be in [1, 65535]")
        if self.store != "memory":
            raise DomainError("only the memory store is available in version 0.1.0")

    @classmethod
    def from_environment(cls) -> Settings:
        return cls(
            environment=os.getenv("EMF_ENVIRONMENT", "development"),
            host=os.getenv("EMF_HOST", "127.0.0.1"),
            port=int(os.getenv("EMF_PORT", "38089")),
            log_level=os.getenv("EMF_LOG_LEVEL", "INFO").upper(),
            store=os.getenv("EMF_STORE", "memory"),
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
