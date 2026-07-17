from __future__ import annotations

import pytest

import evolvable_memory.main as main_module
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
    monkeypatch.setenv("EMF_PUBLIC_API_URL", "http://api.internal:39000/base")

    backend = Settings.from_environment()
    frontend = FrontendSettings.from_environment()

    assert backend == Settings(
        environment="test",
        host="0.0.0.0",
        port=39000,
        log_level="DEBUG",
        store="memory",
        public_api_url="http://api.internal:39000/base",
    )
    assert frontend == FrontendSettings(
        host="0.0.0.0",
        port=34000,
        public_api_url="http://api.internal:39000/base",
    )


def test_invalid_service_settings_are_rejected() -> None:
    with pytest.raises(DomainError, match="port"):
        Settings(port=0)
    with pytest.raises(DomainError, match="DATABASE_URL"):
        Settings(store="postgres")
    with pytest.raises(DomainError, match="either"):
        Settings(store="unknown")
    with pytest.raises(DomainError, match="max_size"):
        Settings(database_pool_min_size=3, database_pool_max_size=2)
    with pytest.raises(DomainError, match="readiness_timeout"):
        Settings(database_readiness_timeout_seconds=0)
    with pytest.raises(DomainError, match="max_request_body_bytes"):
        Settings(max_request_body_bytes=0)
    with pytest.raises(DomainError, match="auth_mode"):
        Settings(auth_mode="none")
    with pytest.raises(DomainError, match="forbidden"):
        Settings(environment="production", auth_mode="development")
    with pytest.raises(DomainError, match="environment"):
        Settings(environment="demo", auth_mode="development")
    with pytest.raises(DomainError, match="issuer"):
        Settings(auth_mode="jwt")
    with pytest.raises(DomainError, match="asymmetric"):
        Settings(
            auth_mode="jwt",
            auth_jwt_issuer="https://identity.example",
            auth_jwt_audience="memory-api",
            auth_jwt_jwks_url="https://identity.example/jwks.json",
            auth_jwt_algorithms=("HS256",),
            auth_audit_hmac_key="x" * 32,
        )
    with pytest.raises(DomainError, match="audit HMAC"):
        Settings(
            auth_mode="jwt",
            auth_jwt_issuer="https://identity.example",
            auth_jwt_audience="memory-api",
            auth_jwt_jwks_url="https://identity.example/jwks.json",
            auth_audit_hmac_key="short",
        )
    with pytest.raises(DomainError, match="issuer must use HTTPS"):
        Settings(
            environment="production",
            auth_mode="jwt",
            auth_jwt_issuer="http://identity.example",
            auth_jwt_audience="memory-api",
            auth_jwt_jwks_url="https://identity.example/jwks.json",
            auth_audit_hmac_key="x" * 32,
        )
    with pytest.raises(DomainError, match="JWKS URL must use HTTPS"):
        Settings(
            environment="production",
            auth_mode="jwt",
            auth_jwt_issuer="https://identity.example",
            auth_jwt_audience="memory-api",
            auth_jwt_jwks_url="http://identity.example/jwks.json",
            auth_audit_hmac_key="x" * 32,
        )
    with pytest.raises(DomainError, match="frontend port"):
        FrontendSettings(port=65_536)
    with pytest.raises(DomainError, match="public_api_url"):
        FrontendSettings(public_api_url="javascript:alert(1)")
    with pytest.raises(DomainError, match="public_api_url"):
        FrontendSettings(public_api_url="https://api.example\r\nX-Injected: true")
    with pytest.raises(DomainError, match="frontend_url"):
        Settings(frontend_url="//memory.example")
    with pytest.raises(DomainError, match="origins without paths"):
        Settings(cors_origins=("https://memory.example/console",))


def test_jwt_authorization_settings_are_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMF_ENVIRONMENT", "production")
    monkeypatch.setenv("EMF_AUTH_MODE", "jwt")
    monkeypatch.setenv("EMF_AUTH_JWT_ISSUER", "https://identity.example")
    monkeypatch.setenv("EMF_AUTH_JWT_AUDIENCE", "memory-api")
    monkeypatch.setenv("EMF_AUTH_JWT_JWKS_URL", "https://identity.example/jwks.json")
    monkeypatch.setenv("EMF_AUTH_JWT_ALGORITHMS", "RS256,ES256")
    monkeypatch.setenv("EMF_AUTH_REQUIRED_SCOPE", "memory.read")
    monkeypatch.setenv("EMF_AUTH_AUDIT_HMAC_KEY", "x" * 32)

    settings = Settings.from_environment()

    assert settings.auth_mode == "jwt"
    assert settings.auth_jwt_algorithms == ("RS256", "ES256")
    assert settings.auth_required_scope == "memory.read"


def test_postgres_settings_and_runtime_urls_are_loaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMF_STORE", "postgres")
    monkeypatch.setenv("EMF_DATABASE_URL", "postgresql://user:secret@db/memory")
    monkeypatch.setenv("EMF_DATABASE_POOL_MIN_SIZE", "2")
    monkeypatch.setenv("EMF_DATABASE_POOL_MAX_SIZE", "8")
    monkeypatch.setenv("EMF_DATABASE_READINESS_TIMEOUT_SECONDS", "0.75")
    monkeypatch.setenv("EMF_MAX_REQUEST_BODY_BYTES", "2097152")
    monkeypatch.setenv("EMF_FRONTEND_URL", "https://memory.example")
    monkeypatch.setenv("EMF_PUBLIC_API_URL", "https://api.example")
    monkeypatch.setenv(
        "EMF_CORS_ORIGINS",
        "https://memory.example, https://admin.example",
    )

    settings = Settings.from_environment()

    assert settings.store == "postgres"
    assert settings.database_pool_min_size == 2
    assert settings.database_pool_max_size == 8
    assert settings.database_readiness_timeout_seconds == 0.75
    assert settings.max_request_body_bytes == 2_097_152
    assert settings.frontend_url == "https://memory.example"
    assert settings.public_api_url == "https://api.example"
    assert settings.cors_origins == (
        "https://memory.example",
        "https://admin.example",
    )


def test_milvus_projection_settings_are_loaded_and_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EMF_STORE", "postgres")
    monkeypatch.setenv("EMF_DATABASE_URL", "postgresql://user:secret@db/memory")
    monkeypatch.setenv("EMF_PROJECTION_MODE", "milvus")
    monkeypatch.setenv("EMF_PROJECTION_REQUIRED", "true")
    monkeypatch.setenv("EMF_MILVUS_URI", "http://milvus:19530")
    monkeypatch.setenv("EMF_MILVUS_COLLECTION", "memory_v2")
    monkeypatch.setenv("EMF_EMBEDDING_PROVIDER", "openai_compatible")
    monkeypatch.setenv("EMF_EMBEDDING_MODEL", "embedding-v3")
    monkeypatch.setenv("EMF_EMBEDDING_DIMENSIONS", "1024")

    settings = Settings.from_environment()

    assert settings.projection_mode == "milvus"
    assert settings.projection_required is True
    assert settings.milvus_uri == "http://milvus:19530"
    assert settings.milvus_collection == "memory_v2"
    assert settings.embedding_provider == "openai_compatible"
    assert settings.embedding_model == "embedding-v3"
    assert settings.embedding_dimensions == 1024

    with pytest.raises(DomainError, match="requires EMF_STORE=postgres"):
        Settings(projection_mode="milvus")
    with pytest.raises(DomainError, match="disabled projection"):
        Settings(projection_required=True)
    with pytest.raises(DomainError, match="boolean"):
        monkeypatch.setenv("EMF_PROJECTION_REQUIRED", "perhaps")
        Settings.from_environment()


def test_backend_entrypoint_disables_query_bearing_uvicorn_access_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(_app: object, **options: object) -> None:
        captured.update(options)

    monkeypatch.setenv("EMF_STORE", "memory")
    monkeypatch.setenv("EMF_LOG_LEVEL", "INFO")
    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)

    main_module.run()

    assert captured["access_log"] is False
    log_config = captured["log_config"]
    assert isinstance(log_config, dict)
    for logger_name in (
        "evolvable_memory.access",
        "evolvable_memory.authorization",
        "evolvable_memory.error",
        "evolvable_memory.projection",
    ):
        assert log_config["loggers"][logger_name] == {
            "handlers": ["emf_json"],
            "level": "INFO",
            "propagate": False,
        }
