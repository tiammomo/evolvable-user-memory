from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from conftest import Harness
from evolvable_memory.adapters.authorization import (
    InMemoryAuthorizationAuditSink,
    LoggingAuthorizationAuditSink,
    RolePolicyAuthorizer,
)
from evolvable_memory.api.app import create_app
from evolvable_memory.api.security import (
    DevelopmentIdentityResolver,
    JwtIdentityResolver,
    PyJwkSigningKeyProvider,
    actor_from_claims,
)
from evolvable_memory.application.security import (
    AccessGrant,
    ActorContext,
    AuthenticationError,
    AuthorizationAuditEvent,
    AuthorizationDecision,
    AuthorizationRequest,
    MemoryAction,
    PrincipalKind,
    ProtectedResource,
)
from evolvable_memory.config import Settings
from evolvable_memory.domain.common import Scope

ALICE_SCOPE = Scope("tenant-a", "alice")


class StaticSigningKeyProvider:
    def __init__(self, public_key: object) -> None:
        self._public_key = public_key

    def key_for(self, token: str) -> object:
        del token
        return self._public_key


class FailingAuthorizationAuditSink:
    def record(self, event: AuthorizationAuditEvent) -> None:
        del event
        raise RuntimeError("audit store unavailable")


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65_537, key_size=2_048)


def test_role_policy_combines_action_scope_and_purpose_without_role_escalation() -> None:
    authorizer = RolePolicyAuthorizer(decision_ids=lambda: UUID(int=1))
    reader = _actor(role="memory_reader")

    allowed = authorizer.decide(_authorization_request(reader, MemoryAction.BELIEF_READ_CURRENT))
    compression_allowed = authorizer.decide(
        _authorization_request(reader, MemoryAction.PROJECTION_COMPRESS)
    )
    write_denied = authorizer.decide(_authorization_request(reader, MemoryAction.EVIDENCE_INGEST))
    strategy_compression_denied = authorizer.decide(
        _authorization_request(
            _actor(role="strategy_operator"),
            MemoryAction.PROJECTION_COMPRESS,
        )
    )
    purpose_denied = authorizer.decide(
        _authorization_request(
            reader,
            MemoryAction.BELIEF_READ_CURRENT,
            purpose="model-training",
        )
    )
    scope_denied = authorizer.decide(
        _authorization_request(
            reader,
            MemoryAction.BELIEF_READ_CURRENT,
            scope=Scope("tenant-a", "bob"),
        )
    )
    tenant_denied = authorizer.decide(
        _authorization_request(
            reader,
            MemoryAction.BELIEF_READ_CURRENT,
            scope=Scope("tenant-b", "alice"),
        )
    )
    admin_read_denied = authorizer.decide(
        _authorization_request(
            _actor(role="tenant_admin"),
            MemoryAction.BELIEF_READ_CURRENT,
        )
    )
    split_grant_denied = authorizer.decide(
        _authorization_request(
            ActorContext(
                principal_id="principal-a",
                kind=PrincipalKind.USER,
                grants=(
                    AccessGrant(
                        tenant_id="tenant-a",
                        subject_ids=("alice",),
                        roles=("memory_reader",),
                        purposes=("model-training",),
                    ),
                    AccessGrant(
                        tenant_id="tenant-a",
                        subject_ids=("alice",),
                        roles=("unknown-role",),
                        purposes=("personalization",),
                    ),
                ),
                authentication_method="test",
            ),
            MemoryAction.BELIEF_READ_CURRENT,
        )
    )

    assert allowed.allowed is True
    assert compression_allowed.allowed is True
    assert allowed.reason == "explicit_grant"
    assert write_denied.reason == "action_not_granted"
    assert strategy_compression_denied.reason == "action_not_granted"
    assert purpose_denied.reason == "purpose_not_granted"
    assert scope_denied.reason == "scope_not_granted"
    assert tenant_denied.reason == "scope_not_granted"
    assert admin_read_denied.reason == "action_not_granted"
    assert split_grant_denied.reason == "purpose_not_granted"


def test_identity_adapters_normalize_trusted_claims_and_local_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_actor = DevelopmentIdentityResolver().authenticate(None)
    assert local_actor.principal_id == "development-console"
    assert local_actor.grants[0].roles == ("development_admin",)

    actor = actor_from_claims(
        {
            "sub": " service-principal ",
            "scope": ["openid", "memory"],
            "principal_type": "service",
            "azp": "worker-client",
            "jti": "token-42",
            "memory_access": [
                {
                    "tenant_id": " tenant-a ",
                    "subject_ids": [" alice "],
                    "roles": [" service_agent "],
                    "purposes": [" personalization "],
                }
            ],
        },
        required_scope="memory",
    )
    assert actor.principal_id == "service-principal"
    assert actor.kind is PrincipalKind.SERVICE
    assert actor.client_id == "worker-client"
    assert actor.grants[0].covers(ALICE_SCOPE)

    provider = PyJwkSigningKeyProvider("https://identity.example/jwks.json")

    class FakeJwkClient:
        def get_signing_key_from_jwt(self, token: str) -> SimpleNamespace:
            assert token == "signed-token"
            return SimpleNamespace(key="public-key")

    monkeypatch.setattr(provider, "_client", FakeJwkClient())
    assert provider.key_for("signed-token") == "public-key"


@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"sub": ""},
        {"scope": 42},
        {"scope": "openid"},
        {"memory_access": None},
        {"memory_access": ["not-an-object"]},
        {"principal_type": "robot"},
        {"client_id": 42},
        {"jti": ""},
        {
            "memory_access": [
                {
                    "tenant_id": "tenant-a",
                    "subject_ids": ["alice"],
                    "roles": [42],
                    "purposes": ["personalization"],
                }
            ]
        },
    ],
)
def test_invalid_identity_claims_are_rejected(
    claim_overrides: dict[str, object],
) -> None:
    claims: dict[str, object] = {
        "sub": "principal-a",
        "scope": "memory",
        "memory_access": [
            {
                "tenant_id": "tenant-a",
                "subject_ids": ["alice"],
                "roles": ["memory_reader"],
                "purposes": ["personalization"],
            }
        ],
    }
    claims.update(claim_overrides)

    with pytest.raises(AuthenticationError):
        actor_from_claims(claims, required_scope="memory")


def test_authorization_audit_rejects_weak_pseudonymization_key() -> None:
    with pytest.raises(ValueError, match="at least 16 bytes"):
        LoggingAuthorizationAuditSink(b"too-short")


def test_jwt_authentication_and_api_authorization_fail_closed(
    harness: Harness,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    audit = InMemoryAuthorizationAuditSink()
    resolver = JwtIdentityResolver(
        issuer="https://identity.example",
        audience="evolvable-memory-api",
        algorithms=("RS256",),
        required_scope="memory",
        key_provider=StaticSigningKeyProvider(signing_key.public_key()),
    )
    client = TestClient(
        create_app(
            harness.app,
            settings=_jwt_settings(),
            clock=harness.clock,
            identity_resolver=resolver,
            authorization_audit=audit,
        )
    )
    writer_token = _token(signing_key, role="memory_operator")
    headers = {"Authorization": f"Bearer {writer_token}"}

    allowed = client.post(
        "/v1/preferences",
        headers=headers,
        json=_preference_payload(),
    )
    forged_scope = client.post(
        "/v1/preferences",
        headers=headers,
        json={**_preference_payload(), "subject_id": "bob"},
    )
    wrong_purpose = client.post(
        "/v1/preferences",
        headers=headers,
        json={**_preference_payload(), "purpose": "model-training"},
    )
    reader_token = _token(signing_key, role="memory_reader")
    reader_write = client.post(
        "/v1/preferences",
        headers={"Authorization": f"Bearer {reader_token}"},
        json=_preference_payload(),
    )
    missing_token = client.post("/v1/preferences", json=_preference_payload())
    wrong_audience = client.post(
        "/v1/preferences",
        headers={
            "Authorization": (
                f"Bearer {_token(signing_key, role='memory_operator', audience='other-api')}"
            )
        },
        json=_preference_payload(),
    )
    wrong_type = client.post(
        "/v1/preferences",
        headers={
            "Authorization": f"Bearer {_token(signing_key, role='memory_operator', typ='JWT')}"
        },
        json=_preference_payload(),
    )
    malformed_grant_token = _token(
        signing_key,
        role="memory_operator",
        claim_overrides={
            "memory_access": [
                {
                    "tenant_id": "tenant-a",
                    "subject_ids": [],
                    "roles": ["memory_operator"],
                    "purposes": ["personalization"],
                }
            ]
        },
    )
    malformed_grant = client.post(
        "/v1/preferences",
        headers={"Authorization": f"Bearer {malformed_grant_token}"},
        json=_preference_payload(),
    )
    expired_token = _token(
        signing_key,
        role="memory_operator",
        claim_overrides={"exp": datetime.now(tz=UTC) - timedelta(minutes=1)},
    )
    expired = client.post(
        "/v1/preferences",
        headers={"Authorization": f"Bearer {expired_token}"},
        json=_preference_payload(),
    )
    wildcard_token = _token(
        signing_key,
        role="memory_operator",
        claim_overrides={
            "memory_access": [
                {
                    "tenant_id": "tenant-a",
                    "subject_ids": ["*"],
                    "roles": ["memory_operator"],
                    "purposes": ["personalization"],
                }
            ]
        },
    )
    wildcard_grant = client.post(
        "/v1/preferences",
        headers={"Authorization": f"Bearer {wildcard_token}"},
        json=_preference_payload(),
    )
    forged_signature_token = _token(
        rsa.generate_private_key(public_exponent=65_537, key_size=2_048),
        role="memory_operator",
    )
    forged_signature = client.post(
        "/v1/preferences",
        headers={"Authorization": f"Bearer {forged_signature_token}"},
        json=_preference_payload(),
    )

    assert allowed.status_code == 201
    assert forged_scope.status_code == 404
    assert forged_scope.json()["error"] == "NotFoundError"
    assert wrong_purpose.status_code == 403
    assert reader_write.status_code == 403
    assert missing_token.status_code == 401
    assert missing_token.headers["www-authenticate"] == "Bearer"
    assert wrong_audience.status_code == 401
    assert wrong_type.status_code == 401
    assert malformed_grant.status_code == 401
    assert expired.status_code == 401
    assert wildcard_grant.status_code == 401
    assert forged_signature.status_code == 401
    assert [event.decision.reason for event in audit.events] == [
        "explicit_grant",
        "scope_not_granted",
        "purpose_not_granted",
        "action_not_granted",
    ]
    assert len(harness.app.list_preferences(Scope("tenant-a", "alice"))) == 1
    assert harness.app.list_preferences(Scope("tenant-a", "bob")) == ()


def test_authorization_audit_failure_prevents_memory_mutation(harness: Harness) -> None:
    client = TestClient(
        create_app(
            harness.app,
            clock=harness.clock,
            authorization_audit=FailingAuthorizationAuditSink(),
        )
    )

    response = client.post("/v1/preferences", json=_preference_payload())

    assert response.status_code == 500
    assert response.json()["error"] == "InternalServerError"
    assert harness.app.list_preferences(ALICE_SCOPE) == ()


def test_authorization_audit_log_is_pseudonymous_and_contains_no_resource_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    actor = ActorContext(
        principal_id="principal-sensitive",
        kind=PrincipalKind.SERVICE,
        grants=(
            AccessGrant(
                tenant_id="tenant-secret",
                subject_ids=("subject-secret",),
                roles=("service_agent",),
                purposes=("personalization",),
            ),
        ),
        authentication_method="oauth2_jwt",
        client_id="client-sensitive",
    )
    resource = ProtectedResource(
        scope=Scope("tenant-secret", "subject-secret"),
        plane=MemoryAction.PROJECTION_RECALL.plane,
        resource_id="record-sensitive",
    )
    event = AuthorizationAuditEvent(
        decision=AuthorizationDecision(
            id=UUID(int=9),
            allowed=True,
            reason="explicit_grant",
            policy_version="test-v1",
        ),
        actor=actor,
        action=MemoryAction.PROJECTION_RECALL,
        resource=resource,
        purpose="personalization",
        request_id="request-9",
        recorded_at=datetime(2026, 7, 15, tzinfo=UTC),
    )
    sink = LoggingAuthorizationAuditSink(b"test-audit-key-with-at-least-32-bytes")

    with caplog.at_level(logging.INFO, logger="evolvable_memory.authorization"):
        sink.record(event)

    record = caplog.records[-1].getMessage()
    assert all(
        sensitive not in record
        for sensitive in (
            "principal-sensitive",
            "tenant-secret",
            "subject-secret",
            "record-sensitive",
            "client-sensitive",
        )
    )
    payload = json.loads(record)
    assert payload["allowed"] is True
    assert payload["action"] == "projection.recall"
    assert payload["request_id"] == "request-9"
    assert len(payload["principal_ref"]) == 24


def _actor(*, role: str) -> ActorContext:
    return ActorContext(
        principal_id="principal-a",
        kind=PrincipalKind.USER,
        grants=(
            AccessGrant(
                tenant_id="tenant-a",
                subject_ids=("alice",),
                roles=(role,),
                purposes=("personalization",),
            ),
        ),
        authentication_method="test",
    )


def _authorization_request(
    actor: ActorContext,
    action: MemoryAction,
    *,
    scope: Scope = ALICE_SCOPE,
    purpose: str = "personalization",
) -> AuthorizationRequest:
    return AuthorizationRequest(
        actor=actor,
        action=action,
        resource=ProtectedResource(scope=scope, plane=action.plane),
        purpose=purpose,
    )


def _jwt_settings() -> Settings:
    return Settings(
        environment="test",
        auth_mode="jwt",
        auth_jwt_issuer="https://identity.example",
        auth_jwt_audience="evolvable-memory-api",
        auth_jwt_jwks_url="https://identity.example/.well-known/jwks.json",
        auth_audit_hmac_key="a" * 32,
    )


def _token(
    key: rsa.RSAPrivateKey,
    *,
    role: str,
    audience: str = "evolvable-memory-api",
    typ: str = "at+jwt",
    claim_overrides: dict[str, object] | None = None,
) -> str:
    now = datetime.now(tz=UTC)
    claims: dict[str, object] = {
        "iss": "https://identity.example",
        "aud": audience,
        "sub": "principal-a",
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "jti": "token-1",
        "scope": "memory",
        "principal_type": "user",
        "memory_access": [
            {
                "tenant_id": "tenant-a",
                "subject_ids": ["alice"],
                "roles": [role],
                "purposes": ["personalization"],
            }
        ],
    }
    if claim_overrides is not None:
        claims.update(claim_overrides)
    return jwt.encode(
        claims,
        key,
        algorithm="RS256",
        headers={"typ": typ, "kid": "test-key"},
    )


def _preference_payload() -> dict[str, object]:
    return {
        "tenant_id": "tenant-a",
        "subject_id": "alice",
        "source": "conversation",
        "idempotency_key": "authorization-test",
        "key": "drink.preference",
        "value": "decaf coffee",
        "context": {"time_of_day": "evening"},
        "evidence_text": "I prefer decaf coffee",
        "confidence": 0.9,
        "purpose": "personalization",
    }
