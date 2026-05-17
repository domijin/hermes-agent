"""BetaTenantResolver — maps (platform, raw_user_id) → TenantContext.

Resolution order (BlueBubbles beta):

1. Operator phone (``OPERATOR_PHONE``) → ``op_self`` / operator / open.
2. Allowlist entry — preferred. Carries a pre-assigned ``tenant_id``,
   ``alias``, and ``trust_tier``.
3. HMAC-SHA256(platform + raw_user_id, secret)[:12] for allowlisted users
   without a pre-assigned tenant_id — keeps the resolver functional before
   the signup DB backfills tenant_ids.
4. Unknown user → ``TenantContext.unknown()``. Fail-closed.

The HMAC input mixes ``platform`` and ``raw_user_id`` so the same phone on
iMessage vs Telegram resolves to distinct tenants.

See: runs/imessage-beta-worker/specs/tenant-isolation-implementation.md (Spec 1)
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import Mapping, Optional, Tuple

from .tenant_context import TenantContext


logger = logging.getLogger(__name__)


# Operator's iMessage handle. Hard-coded — there is exactly one operator per
# Hermes-lite deployment and the value is part of the host's identity.
OPERATOR_PHONE = "+19565452817"

# Length of the hex slice we keep from the HMAC output. 12 hex chars = 48
# bits, which is comfortably more than enough to avoid collisions at the
# scale of "all beta users this server will ever see" and fits cleanly in a
# session key.
_TENANT_HEX_LEN = 12


AllowlistKey = Tuple[str, str]
AllowlistEntry = Mapping[str, str]
Allowlist = Mapping[AllowlistKey, AllowlistEntry]


class BetaTenantResolver:
    """Resolve raw (platform, user_id) tuples to TenantContext objects."""

    def __init__(
        self,
        allowlist: Optional[Allowlist] = None,
        secret: Optional[str] = None,
    ) -> None:
        """
        Args:
            allowlist: ``{(platform, raw_user_id): {"alias": ..., "trust_tier": ...,
                       "tenant_id": ...?}}``. Entries without ``tenant_id``
                       fall through to the HMAC derivation.
            secret:    HMAC secret. Required for HMAC derivation; resolver
                       still works without it for operator + entries that
                       carry pre-assigned tenant_ids.
        """
        self._allowlist: Allowlist = allowlist or {}
        self._secret: bytes = (secret or "").encode("utf-8")

    # ------------------------------------------------------------------ API

    def resolve(self, platform: str, raw_user_id: str) -> TenantContext:
        """Map a raw caller identity to a TenantContext.

        Never raises — unknown callers get ``TenantContext.unknown()``.
        """
        # 1. Operator hardwire — keep operator UX untouched by beta_mode.
        if platform == "bluebubbles" and raw_user_id == OPERATOR_PHONE:
            return TenantContext(
                tenant_id="op_self",
                platform=platform,
                external_user_ref=raw_user_id,
                display_alias="Operator",
                trust_tier="operator",
                privacy_mode="open",
            )

        # 2 & 3. Allowlist lookup (with optional pre-assigned tenant_id).
        entry = self._allowlist.get((platform, raw_user_id))
        if entry is not None:
            tenant_id = entry.get("tenant_id") or self._derive_tenant_id(platform, raw_user_id)
            return TenantContext(
                tenant_id=tenant_id,
                platform=platform,
                external_user_ref=raw_user_id,
                display_alias=entry.get("alias") or f"User {tenant_id[-2:]}",
                trust_tier=entry.get("trust_tier") or "beta",
                privacy_mode="strict",
            )

        # 4. Unknown caller — fail-closed.
        return TenantContext.unknown()

    # ---------------------------------------------------------------- HMAC

    def _derive_tenant_id(self, platform: str, raw_user_id: str) -> str:
        """Derive ``btu_<12hex>`` from HMAC-SHA256(platform|raw_id).

        Mixing ``platform`` into the message prevents collisions when the
        same raw_user_id (e.g. an E.164 phone number) shows up on two
        different platforms — they must resolve to distinct tenants.
        """
        msg = f"{platform}|{raw_user_id}".encode("utf-8")
        digest = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
        return f"btu_{digest[:_TENANT_HEX_LEN]}"


__all__ = ["BetaTenantResolver", "OPERATOR_PHONE", "build_tenant_resolver_from_env"]


# ---------------------------------------------------------------------------
# Env-driven factory (Hermes-lite gateway bootstrap)
# ---------------------------------------------------------------------------

def _parse_allowed_users(raw: str) -> list[str]:
    """Split a comma-separated allowlist, trimming whitespace and dropping empties."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _load_or_generate_secret() -> str:
    """Resolve the HMAC secret, generating + persisting one on first use.

    Resolution order:
      1. ``HERMES_TENANT_HMAC_SECRET`` env var.
      2. File at ``HERMES_TENANT_SECRET_PATH`` (or
         ``~/.hermes/tenant.secret`` by default).
      3. Generate a fresh ``secrets.token_hex(32)`` and persist it to that
         file with mode 0600.

    Generating-on-missing keeps the resolver functional out of the box. The
    secret is per-host — rotating it changes every tenant_id, so we treat it
    as write-once.
    """
    env = os.environ.get("HERMES_TENANT_HMAC_SECRET", "").strip()
    if env:
        return env

    secret_path_str = os.environ.get("HERMES_TENANT_SECRET_PATH") or str(
        Path.home() / ".hermes" / "tenant.secret"
    )
    secret_path = Path(secret_path_str)

    if secret_path.exists():
        try:
            value = secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value
        except OSError as exc:
            logger.warning("tenant_resolver: cannot read %s: %s", secret_path, exc)

    # Generate + persist.
    new_secret = secrets.token_hex(32)
    try:
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_path.write_text(new_secret, encoding="utf-8")
        try:
            secret_path.chmod(0o600)
        except OSError:
            pass
        logger.info("tenant_resolver: generated new HMAC secret at %s", secret_path)
    except OSError as exc:
        logger.warning(
            "tenant_resolver: could not persist generated secret to %s: %s — "
            "tenant_ids will change on restart",
            secret_path,
            exc,
        )
    return new_secret


def build_tenant_resolver_from_env() -> "BetaTenantResolver":
    """Construct a BetaTenantResolver from the running gateway's env.

    Reads:
      - ``BLUEBUBBLES_ALLOWED_USERS`` (comma-separated phones) → beta allowlist
      - ``HERMES_TENANT_HMAC_SECRET`` (or generated/persisted secret file) → HMAC key

    The resolver is safe to construct even when no allowlist is configured —
    every non-operator caller will simply fall through to
    ``TenantContext.unknown()``.
    """
    allowlist: dict[Tuple[str, str], dict] = {}

    raw_bb = os.environ.get("BLUEBUBBLES_ALLOWED_USERS", "")
    for phone in _parse_allowed_users(raw_bb):
        # Skip the operator — they're hardwired in the resolver.
        if phone == OPERATOR_PHONE:
            continue
        allowlist[("bluebubbles", phone)] = {
            "alias": "",  # no alias yet — display layer falls back to "User <id>"
            "trust_tier": "beta",
        }

    secret = _load_or_generate_secret()
    return BetaTenantResolver(allowlist=allowlist, secret=secret)
