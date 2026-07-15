from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

import jwt
from jwt import PyJWKClient

from evolvable_memory.application.security import (
    AccessGrant,
    ActorContext,
    AuthenticationError,
    PrincipalKind,
)


class IdentityResolver(Protocol):
    def authenticate(self, token: str | None) -> ActorContext: ...


class SigningKeyProvider(Protocol):
    def key_for(self, token: str) -> Any: ...


class DevelopmentIdentityResolver:
    """Local-only identity adapter; it must never be enabled in production."""

    def authenticate(self, token: str | None) -> ActorContext:
        del token
        return ActorContext(
            principal_id="development-console",
            kind=PrincipalKind.USER,
            grants=(
                AccessGrant(
                    tenant_id="*",
                    subject_ids=("*",),
                    roles=("development_admin",),
                    purposes=("*",),
                ),
            ),
            authentication_method="development",
            client_id="memory-console",
        )


class PyJwkSigningKeyProvider:
    def __init__(self, jwks_url: str) -> None:
        self._client = PyJWKClient(jwks_url, timeout=5.0, lifespan=300)

    def key_for(self, token: str) -> Any:
        return self._client.get_signing_key_from_jwt(token).key


class JwtIdentityResolver:
    """Validate an RFC 9068-style JWT access token and map trusted grants."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        algorithms: tuple[str, ...],
        required_scope: str,
        key_provider: SigningKeyProvider,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._algorithms = algorithms
        self._required_scope = required_scope
        self._key_provider = key_provider

    def authenticate(self, token: str | None) -> ActorContext:
        if token is None:
            raise AuthenticationError("bearer access token is required")
        try:
            header = jwt.get_unverified_header(token)
            if header.get("typ") not in {"at+jwt", "application/at+jwt"}:
                raise AuthenticationError("token is not an access token")
            key = self._key_provider.key_for(token)
            claims = jwt.decode(
                token,
                key=key,
                algorithms=list(self._algorithms),
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["iss", "aud", "sub", "exp"]},
            )
        except AuthenticationError:
            raise
        except Exception as exc:
            raise AuthenticationError("access token validation failed") from exc
        return actor_from_claims(claims, required_scope=self._required_scope)


def actor_from_claims(
    claims: Mapping[str, object],
    *,
    required_scope: str,
) -> ActorContext:
    principal_id = _required_text_claim(claims, "sub")
    token_scopes = _scope_claim(claims.get("scope"))
    if required_scope not in token_scopes:
        raise AuthenticationError("access token is missing the required API scope")

    raw_access = claims.get("memory_access")
    if not isinstance(raw_access, list) or not raw_access:
        raise AuthenticationError("access token has no memory access grants")
    grants = tuple(_access_grant(item) for item in raw_access)

    raw_kind = claims.get("principal_type", PrincipalKind.USER.value)
    try:
        kind = PrincipalKind(str(raw_kind))
    except ValueError as exc:
        raise AuthenticationError("access token principal type is invalid") from exc

    client_id = _optional_text_claim(claims, "client_id")
    if client_id is None:
        client_id = _optional_text_claim(claims, "azp")
    return ActorContext(
        principal_id=principal_id,
        kind=kind,
        grants=grants,
        authentication_method="oauth2_jwt",
        client_id=client_id,
        token_id=_optional_text_claim(claims, "jti"),
    )


def _access_grant(value: object) -> AccessGrant:
    if not isinstance(value, dict):
        raise AuthenticationError("memory access grant must be an object")
    try:
        grant = AccessGrant(
            tenant_id=_required_text_claim(value, "tenant_id"),
            subject_ids=_text_list_claim(value, "subject_ids"),
            roles=_text_list_claim(value, "roles"),
            purposes=_text_list_claim(value, "purposes"),
        )
    except AuthenticationError:
        raise
    except ValueError as exc:
        raise AuthenticationError("memory access grant is invalid") from exc
    if grant.tenant_id == "*" or "*" in grant.subject_ids or "*" in grant.purposes:
        raise AuthenticationError("JWT memory access grants must use explicit scope")
    return grant


def _required_text_claim(claims: Mapping[str, object], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationError(f"access token claim {name} is missing or invalid")
    return value.strip()


def _optional_text_claim(claims: Mapping[str, object], name: str) -> str | None:
    value = claims.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AuthenticationError(f"access token claim {name} is invalid")
    return value.strip()


def _text_list_claim(claims: Mapping[str, object], name: str) -> tuple[str, ...]:
    value = claims.get(name)
    if not isinstance(value, list) or not value:
        raise AuthenticationError(f"access token claim {name} is missing or invalid")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise AuthenticationError(f"access token claim {name} is invalid")
        normalized.append(item.strip())
    return tuple(normalized)


def _scope_claim(value: object) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset(part for part in value.split() if part)
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return frozenset(str(part).strip() for part in value if str(part).strip())
    raise AuthenticationError("access token scope claim is missing or invalid")
