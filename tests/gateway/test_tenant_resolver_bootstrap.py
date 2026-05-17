"""RED tests for Phase 1c — GatewayRunner._resolve_tenant + shadow-stage push.

These lock the contract for the resolver bootstrap and ingress wiring:

- The runner exposes ``_resolve_tenant(source) -> TenantContext`` that never
  raises and always returns a TenantContext (operator/beta/unknown).
- The runner's resolver is built from environment variables
  (BLUEBUBBLES_ALLOWED_USERS + HERMES_TENANT_HMAC_SECRET) at construction
  time. No live DB dependency.
- Shadow stage: the resolver runs but ``build_session_key`` is NOT called
  with ``tenant=``. Behavior is unchanged in production; we only log.

Spec: runs/imessage-beta-worker/specs/tenant-isolation-implementation.md (Spec 1)
"""

from __future__ import annotations

import os

import pytest


# Stable test fixtures.
_BETA_PHONE = "+12025550101"
_OPERATOR_PHONE = "+19565452817"


@pytest.fixture
def tenant_env(monkeypatch):
    """Configure BLUEBUBBLES_ALLOWED_USERS and HMAC secret via env."""
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", _BETA_PHONE)
    monkeypatch.setenv("HERMES_TENANT_HMAC_SECRET", "test-secret-12345")
    yield


def test_build_tenant_resolver_from_env_includes_allowlisted(tenant_env):
    """The factory loads BLUEBUBBLES_ALLOWED_USERS into the resolver allowlist."""
    from gateway.tenant_resolver import build_tenant_resolver_from_env

    resolver = build_tenant_resolver_from_env()
    ctx = resolver.resolve("bluebubbles", _BETA_PHONE)

    assert ctx.trust_tier == "beta"
    assert ctx.tenant_id.startswith("btu_")
    assert _BETA_PHONE not in ctx.tenant_id


def test_build_tenant_resolver_from_env_operator(tenant_env):
    """Operator phone always resolves to op_self regardless of allowlist."""
    from gateway.tenant_resolver import build_tenant_resolver_from_env

    resolver = build_tenant_resolver_from_env()
    ctx = resolver.resolve("bluebubbles", _OPERATOR_PHONE)

    assert ctx.tenant_id == "op_self"
    assert ctx.trust_tier == "operator"


def test_build_tenant_resolver_handles_multiple_allowed_users(monkeypatch):
    """Comma-separated allowlist parses into multiple entries."""
    from gateway.tenant_resolver import build_tenant_resolver_from_env

    monkeypatch.setenv(
        "BLUEBUBBLES_ALLOWED_USERS",
        f"{_BETA_PHONE},+13105550102 , +14155550103",  # spaces tolerated
    )
    monkeypatch.setenv("HERMES_TENANT_HMAC_SECRET", "test-secret-12345")

    resolver = build_tenant_resolver_from_env()
    for phone in (_BETA_PHONE, "+13105550102", "+14155550103"):
        ctx = resolver.resolve("bluebubbles", phone)
        assert ctx.trust_tier == "beta", f"{phone} should resolve to beta"
        assert ctx.tenant_id.startswith("btu_")


def test_build_tenant_resolver_unknown_user(tenant_env):
    """Non-allowlisted user fails closed."""
    from gateway.tenant_resolver import build_tenant_resolver_from_env

    resolver = build_tenant_resolver_from_env()
    ctx = resolver.resolve("bluebubbles", "+19998887777")
    assert ctx.tenant_id == "unknown"
    assert ctx.trust_tier == "unknown"


def test_build_tenant_resolver_generates_secret_if_missing(monkeypatch, tmp_path):
    """When HERMES_TENANT_HMAC_SECRET is missing, generate + persist one.

    This keeps the resolver functional on first run without manual setup. The
    generated secret must be stable across calls within the same process —
    we don't want tenant_ids to change mid-run.
    """
    from gateway.tenant_resolver import build_tenant_resolver_from_env

    monkeypatch.delenv("HERMES_TENANT_HMAC_SECRET", raising=False)
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", _BETA_PHONE)
    # Override secret persistence path to a tmpdir so we don't pollute the
    # real ~/.hermes.
    monkeypatch.setenv("HERMES_TENANT_SECRET_PATH", str(tmp_path / "tenant.secret"))

    r1 = build_tenant_resolver_from_env()
    r2 = build_tenant_resolver_from_env()

    ctx1 = r1.resolve("bluebubbles", _BETA_PHONE)
    ctx2 = r2.resolve("bluebubbles", _BETA_PHONE)
    assert ctx1.tenant_id == ctx2.tenant_id  # stable across resolver instances
    assert ctx1.tenant_id.startswith("btu_")
