"""Canonical TenantContext + per-task contextvar for Hermes-lite tenant isolation.

A ``TenantContext`` is resolved once at gateway ingress (see
``gateway.tenant_resolver``) and threaded through session, memory, media, and
logging via the ``current_tenant`` contextvar. Mirrors the existing
``gateway.session_context`` pattern, which uses ``contextvars.ContextVar`` so
two concurrent asyncio tasks never observe each other's tenant.

Privacy invariants (enforced by callers, locked by tests):

1. ``tenant_id`` is opaque — never contains the raw phone/email.
2. ``external_user_ref`` MAY hold the raw value, but it is admin-only and
   must never enter the model context.
3. ``display_alias`` is model-safe (nickname or "User <hash[:2]>").
4. Unknown/unauthenticated callers resolve to ``TenantContext.unknown()`` so
   downstream code can fail closed.

See: runs/imessage-beta-worker/specs/tenant-isolation-implementation.md (Spec 1)
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class TenantContext:
    """One tenant's identity for the duration of a single request.

    Frozen so accidental mutation mid-request is impossible.

    Fields:
        tenant_id:        Opaque stable id. ``"btu_<12hex>"`` for beta,
                          ``"op_self"`` for the operator, ``"unknown"`` for
                          unresolved callers.
        platform:         Lower-case platform name (``"bluebubbles"``, ...).
        external_user_ref: The raw user id (phone, email, handle). Admin-only.
                          Never include in model prompts.
        display_alias:    Model-safe display name. Falls back to ``"User <id>"``.
        trust_tier:       ``"operator" | "beta" | "blocked" | "unknown"``.
        privacy_mode:     ``"strict"`` (default for beta/unknown) or ``"open"``
                          (operator only).
    """

    tenant_id: str
    platform: str
    external_user_ref: str
    display_alias: str
    trust_tier: str
    privacy_mode: str

    @classmethod
    def unknown(cls) -> "TenantContext":
        """Fail-closed sentinel for unresolved callers.

        Returned by the resolver when a (platform, user_id) tuple is not in
        the allowlist. Callers that touch persistent storage MUST refuse to
        write when ``trust_tier == "unknown"``.
        """
        return cls(
            tenant_id="unknown",
            platform="",
            external_user_ref="",
            display_alias="unknown",
            trust_tier="unknown",
            privacy_mode="strict",
        )


# Per-task contextvar. Mirrors gateway.session_context — task-local so two
# concurrent asyncio tasks (or threads spawned via run_in_executor) get their
# own value with no cross-contamination.
current_tenant: ContextVar[Optional[TenantContext]] = ContextVar(
    "hermes_current_tenant",
    default=None,
)


__all__ = ["TenantContext", "current_tenant"]
