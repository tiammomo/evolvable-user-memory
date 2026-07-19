from __future__ import annotations

API_CONTRACT = "evolvable-memory-http/v1"

# This manifest is intentionally provider-neutral. Consumers negotiate the
# stable HTTP contract instead of importing this package or depending on its
# persistence implementation.
API_CAPABILITIES: tuple[str, ...] = (
    "preference.write",
    "preference.list",
    "preference.correct",
    "preference.history",
    "recall.trace",
    "recall.bitemporal",
    "recall.context-projection",
    "experience.usage-receipt",
    "experience.outcome",
    "governance.processing-grants",
    "governance.suppression",
    "governance.erasure",
    "governance.erasure-receipts",
)


def production_blockers(
    *,
    store: str,
    auth_mode: str,
    governance_mode: str = "development",
    governance_ready: bool = False,
    audit_sink: str = "log",
    audit_ready: bool = False,
    environment: str = "production",
) -> tuple[str, ...]:
    """Return machine-readable blockers without overstating deployment readiness."""
    blockers: list[str] = []
    if governance_mode != "postgres":
        blockers.append("configuration.persistent-governance")
    elif not governance_ready:
        blockers.extend(
            (
                "privacy.processing-grants",
                "privacy.suppression-erasure",
            )
        )
    if audit_sink != "postgres":
        blockers.append("configuration.durable-audit")
    elif not audit_ready:
        blockers.append("audit.durable-sink")
    if store != "postgres":
        blockers.append("authority.durable-storage")
    if auth_mode != "jwt":
        blockers.append("configuration.trusted-jwt")
    if environment not in {"staging", "production"}:
        blockers.append("runtime.production-profile")
    return tuple(blockers)
