from __future__ import annotations

import pytest

from evolvable_memory.config import FrontendSettings, Settings
from evolvable_memory.domain.common import DomainError


def test_default_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ("EMF_PORT", "EMF_FRONTEND_PORT"):
        monkeypatch.delenv(variable, raising=False)

    assert Settings.from_environment().port == 38089
    assert FrontendSettings.from_environment().port == 33009


def test_service_settings_are_loaded_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMF_ENVIRONMENT", "test")
    monkeypatch.setenv("EMF_HOST", "0.0.0.0")
    monkeypatch.setenv("EMF_PORT", "39000")
    monkeypatch.setenv("EMF_LOG_LEVEL", "debug")
    monkeypatch.setenv("EMF_STORE", "memory")
    monkeypatch.setenv("EMF_FRONTEND_HOST", "0.0.0.0")
    monkeypatch.setenv("EMF_FRONTEND_PORT", "34000")

    backend = Settings.from_environment()
    frontend = FrontendSettings.from_environment()

    assert backend == Settings(
        environment="test",
        host="0.0.0.0",
        port=39000,
        log_level="DEBUG",
        store="memory",
    )
    assert frontend == FrontendSettings(host="0.0.0.0", port=34000)


def test_invalid_service_settings_are_rejected() -> None:
    with pytest.raises(DomainError, match="port"):
        Settings(port=0)
    with pytest.raises(DomainError, match="memory store"):
        Settings(store="postgres")
    with pytest.raises(DomainError, match="frontend port"):
        FrontendSettings(port=65_536)
