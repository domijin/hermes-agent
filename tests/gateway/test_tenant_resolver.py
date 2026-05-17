"""RED tests for gateway.tenant_resolver — Phase 1 of Hermes-lite tenant isolation.

These tests are expected to fail until `gateway/tenant_resolver.py` is
implemented. They lock the BetaTenantResolver contract: how a raw (platform,
user_id) tuple from BlueBubbles or another gateway maps to an opaque,
stable TenantContext.

Spec: runs/imessage-beta-worker/specs/tenant-isolation-implementation.md (Spec 1)
"""

from __future__ import annotations

import pytest

# Stable test fixtures — see spec for the operator/beta phone conventions.
_TEST_SECRET = "test-secret-12345"
_TEST_BETA_PHONE = "+12025550101"
_TEST_BETA_ALIAS = "User A"


def _allowlist_with(phone: str, alias: str, tenant_id: str | None = None) -> dict:
    """Build a minimal in-memory allowlist for tests.

    Keyed by (platform, raw_user_id). Values carry the canonical alias and
    optional pre-assigned tenant_id.
    """
    entry: dict = {"alias": alias, "trust_tier": "beta"}
    if tenant_id is not None:
        entry["tenant_id"] = tenant_id
    return {("bluebubbles", phone): entry}


def test_operator_phone_resolves_to_op_self():
    """The operator's own phone resolves to a privileged operator tenant.

    The operator is not a beta user — they must keep full UX (home channels,
    delivery options, admin notice drain) even when beta_mode is on.
    """
    from gateway.tenant_resolver import OPERATOR_PHONE, BetaTenantResolver

    resolver = BetaTenantResolver(allowlist={}, secret=_TEST_SECRET)
    ctx = resolver.resolve("bluebubbles", OPERATOR_PHONE)

    assert ctx.tenant_id == "op_self"
    assert ctx.trust_tier == "operator"
    assert ctx.privacy_mode == "open"


def test_allowlisted_user_gets_stable_btu_id():
    """An allowlisted beta user resolves to a stable, repeatable btu_* id."""
    from gateway.tenant_resolver import BetaTenantResolver

    resolver = BetaTenantResolver(
        allowlist=_allowlist_with(_TEST_BETA_PHONE, _TEST_BETA_ALIAS),
        secret=_TEST_SECRET,
    )

    ctx_a = resolver.resolve("bluebubbles", _TEST_BETA_PHONE)
    ctx_b = resolver.resolve("bluebubbles", _TEST_BETA_PHONE)

    assert ctx_a.tenant_id == ctx_b.tenant_id
    assert ctx_a.tenant_id.startswith("btu_")
    assert ctx_a.trust_tier == "beta"
    assert ctx_a.display_alias == _TEST_BETA_ALIAS


def test_allowlisted_user_id_is_opaque():
    """tenant_id MUST NOT leak the raw phone number.

    This is the core PII invariant — the model and any prompt/log line should
    only ever see opaque tenant_ids, never the raw E.164.
    """
    from gateway.tenant_resolver import BetaTenantResolver

    resolver = BetaTenantResolver(
        allowlist=_allowlist_with(_TEST_BETA_PHONE, _TEST_BETA_ALIAS),
        secret=_TEST_SECRET,
    )
    ctx = resolver.resolve("bluebubbles", _TEST_BETA_PHONE)

    # Raw phone must not appear in the tenant_id, in any form (with or
    # without the leading +, with or without digit substrings).
    assert _TEST_BETA_PHONE not in ctx.tenant_id
    assert _TEST_BETA_PHONE.lstrip("+") not in ctx.tenant_id
    # external_user_ref MAY hold the raw value — it's the admin-only field.
    assert ctx.external_user_ref == _TEST_BETA_PHONE


def test_unknown_user_resolves_to_unknown():
    """A phone not in the allowlist fails closed to the unknown tenant.

    Unknown tenants have privacy_mode='strict' and no persistent storage.
    """
    from gateway.tenant_resolver import BetaTenantResolver

    resolver = BetaTenantResolver(allowlist={}, secret=_TEST_SECRET)
    ctx = resolver.resolve("bluebubbles", "+19998887777")

    assert ctx.tenant_id == "unknown"
    assert ctx.trust_tier == "unknown"
    assert ctx.privacy_mode == "strict"


def test_hmac_fallback_when_db_missing():
    """When an allowlist entry omits a pre-assigned tenant_id, derive via HMAC.

    The HMAC fallback keeps the resolver functional even if the signup DB
    hasn't backfilled tenant_ids yet. The id must still be stable and opaque.
    """
    from gateway.tenant_resolver import BetaTenantResolver

    # Allowlist entry without a pre-assigned tenant_id.
    allowlist = {("bluebubbles", _TEST_BETA_PHONE): {"alias": "User A", "trust_tier": "beta"}}

    resolver = BetaTenantResolver(allowlist=allowlist, secret=_TEST_SECRET)
    ctx_a = resolver.resolve("bluebubbles", _TEST_BETA_PHONE)
    ctx_b = resolver.resolve("bluebubbles", _TEST_BETA_PHONE)

    assert ctx_a.tenant_id == ctx_b.tenant_id
    assert ctx_a.tenant_id.startswith("btu_")
    # Different secret would produce a different id — sanity check.
    resolver_alt = BetaTenantResolver(allowlist=allowlist, secret="different-secret")
    ctx_alt = resolver_alt.resolve("bluebubbles", _TEST_BETA_PHONE)
    assert ctx_alt.tenant_id != ctx_a.tenant_id


def test_different_platforms_get_different_ids():
    """Same raw user_id on different platforms → different tenant_ids.

    A phone number on iMessage is not the same identity as the same string on
    Telegram. The HMAC input mixes platform + raw_id so they cannot collide.
    """
    from gateway.tenant_resolver import BetaTenantResolver

    allowlist = {
        ("bluebubbles", _TEST_BETA_PHONE): {"alias": "User A", "trust_tier": "beta"},
        ("telegram", _TEST_BETA_PHONE): {"alias": "User A on TG", "trust_tier": "beta"},
    }

    resolver = BetaTenantResolver(allowlist=allowlist, secret=_TEST_SECRET)
    ctx_bb = resolver.resolve("bluebubbles", _TEST_BETA_PHONE)
    ctx_tg = resolver.resolve("telegram", _TEST_BETA_PHONE)

    assert ctx_bb.tenant_id != ctx_tg.tenant_id
    assert ctx_bb.platform == "bluebubbles"
    assert ctx_tg.platform == "telegram"
