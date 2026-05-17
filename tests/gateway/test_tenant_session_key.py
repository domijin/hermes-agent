"""RED tests for tenant-aware session key derivation.

These tests are expected to fail until `gateway/session.py::build_session_key`
accepts an optional `tenant` keyword and routes beta tenants through opaque
tenant-keyed paths instead of raw chat_id/user_id strings.

Spec: runs/imessage-beta-worker/specs/tenant-isolation-implementation.md (Spec 1)
"""

from __future__ import annotations

import pytest

from gateway.session import SessionSource, build_session_key
from gateway.config import Platform


_BETA_TENANT_ID = "btu_abc123def4"
_BETA_PHONE = "+12025550101"
_OPERATOR_PHONE = "+19565452817"


def _beta_tenant(tenant_id: str = _BETA_TENANT_ID):
    from gateway.tenant_context import TenantContext

    return TenantContext(
        tenant_id=tenant_id,
        platform="bluebubbles",
        external_user_ref=_BETA_PHONE,
        display_alias="User A",
        trust_tier="beta",
        privacy_mode="strict",
    )


def _operator_tenant():
    from gateway.tenant_context import TenantContext

    return TenantContext(
        tenant_id="op_self",
        platform="bluebubbles",
        external_user_ref=_OPERATOR_PHONE,
        display_alias="Operator",
        trust_tier="operator",
        privacy_mode="open",
    )


def test_beta_dm_session_key_uses_tenant_id():
    """Beta DM session key takes the tenant-keyed form, not chat_id-keyed."""
    source = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id=_BETA_PHONE,
        chat_type="dm",
        user_id=_BETA_PHONE,
    )
    key = build_session_key(source, tenant=_beta_tenant())
    assert key == f"agent:main:bluebubbles:tenant:{_BETA_TENANT_ID}:dm"


def test_beta_session_key_does_not_contain_phone():
    """The raw phone must never appear in the session key for a beta user."""
    source = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id=_BETA_PHONE,
        chat_type="dm",
        user_id=_BETA_PHONE,
    )
    key = build_session_key(source, tenant=_beta_tenant())
    assert _BETA_PHONE not in key
    assert _BETA_PHONE.lstrip("+") not in key


def test_two_beta_users_distinct_session_keys():
    """Different tenant_ids → different session keys, even with same chat shape."""
    src_a = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id=_BETA_PHONE,
        chat_type="dm",
        user_id=_BETA_PHONE,
    )
    src_b = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id="+13105550102",
        chat_type="dm",
        user_id="+13105550102",
    )
    key_a = build_session_key(src_a, tenant=_beta_tenant("btu_alpha000001"))
    key_b = build_session_key(src_b, tenant=_beta_tenant("btu_beta0000002"))
    assert key_a != key_b
    assert "btu_alpha000001" in key_a
    assert "btu_beta0000002" in key_b


def test_operator_session_key_unchanged_by_tenant_feature():
    """Operator tenant must fall back to legacy chat_id-based session key.

    The operator's own chats are not beta-isolated — admin tooling, home
    channel delivery, and existing operator sessions must keep working with
    their current keys.
    """
    source = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id=_OPERATOR_PHONE,
        chat_type="dm",
        user_id=_OPERATOR_PHONE,
    )

    legacy_key = build_session_key(source)
    operator_key = build_session_key(source, tenant=_operator_tenant())

    assert operator_key == legacy_key
    assert "tenant:" not in operator_key
    assert _OPERATOR_PHONE in operator_key


def test_beta_group_session_per_user_by_default_for_beta_tier():
    """Beta group sessions isolate by tenant regardless of global setting.

    Even if a deployment chose `group_sessions_per_user=False` for shared
    group bot UX, beta users must still get per-user isolation — otherwise
    one beta user could see another's group conversation.
    """
    source = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id="group_chat_xyz",
        chat_type="group",
        user_id=_BETA_PHONE,
    )

    key = build_session_key(
        source,
        group_sessions_per_user=False,  # globally shared groups
        tenant=_beta_tenant(),
    )

    # Beta tenant must still be isolated — tenant_id present, raw phone absent.
    assert _BETA_TENANT_ID in key
    assert _BETA_PHONE not in key
