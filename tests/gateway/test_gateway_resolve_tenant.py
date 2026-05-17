"""RED tests for GatewayRunner shadow-stage tenant resolution.

Locks the integration contract:

- ``GatewayRunner`` exposes ``_resolve_tenant(source)`` that returns a
  TenantContext for any source. Returns the unknown sentinel on resolver
  errors — never raises.
- The runner has a ``tenant_resolver`` attribute, lazily built from env.
- The tenant is pushed on ``current_tenant`` via a helper context manager so
  concurrent handlers don't clobber each other.

Spec: runs/imessage-beta-worker/specs/tenant-isolation-implementation.md (Spec 1)
"""

from __future__ import annotations

import asyncio

import pytest


_BETA_PHONE = "+12025550101"
_OPERATOR_PHONE = "+19565452817"


@pytest.fixture
def tenant_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", _BETA_PHONE)
    monkeypatch.setenv("HERMES_TENANT_HMAC_SECRET", "test-secret-12345")
    monkeypatch.setenv("HERMES_TENANT_SECRET_PATH", str(tmp_path / "tenant.secret"))
    yield


def _make_runner_stub():
    """Construct a minimal stand-in for GatewayRunner with just _resolve_tenant.

    We deliberately bypass the heavy ``GatewayRunner.__init__`` (which wires
    every adapter, plugin, and persistent store) — we just need an object
    that mixes in the method under test.
    """
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._tenant_resolver = None  # type: ignore[attr-defined]
    return runner


def _src_bluebubbles(user_id: str):
    from gateway.session import SessionSource
    from gateway.config import Platform

    return SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id=user_id,
        chat_type="dm",
        user_id=user_id,
    )


def test_resolve_tenant_for_operator(tenant_env):
    runner = _make_runner_stub()
    ctx = runner._resolve_tenant(_src_bluebubbles(_OPERATOR_PHONE))
    assert ctx.tenant_id == "op_self"
    assert ctx.trust_tier == "operator"


def test_resolve_tenant_for_beta_user(tenant_env):
    runner = _make_runner_stub()
    ctx = runner._resolve_tenant(_src_bluebubbles(_BETA_PHONE))
    assert ctx.trust_tier == "beta"
    assert ctx.tenant_id.startswith("btu_")
    assert _BETA_PHONE not in ctx.tenant_id


def test_resolve_tenant_for_unknown_user(tenant_env):
    runner = _make_runner_stub()
    ctx = runner._resolve_tenant(_src_bluebubbles("+19998887777"))
    assert ctx.tenant_id == "unknown"
    assert ctx.trust_tier == "unknown"


def test_resolve_tenant_never_raises_on_missing_user_id(tenant_env):
    """If source.user_id is None we still get a TenantContext (unknown)."""
    from gateway.session import SessionSource
    from gateway.config import Platform

    runner = _make_runner_stub()
    src = SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id="",
        chat_type="dm",
        user_id=None,
    )
    ctx = runner._resolve_tenant(src)
    assert ctx.tenant_id == "unknown"


def test_resolver_is_cached_per_runner(tenant_env):
    """_resolve_tenant builds the resolver once and reuses it."""
    runner = _make_runner_stub()
    runner._resolve_tenant(_src_bluebubbles(_BETA_PHONE))
    r1 = runner._tenant_resolver
    runner._resolve_tenant(_src_bluebubbles(_BETA_PHONE))
    r2 = runner._tenant_resolver
    assert r1 is r2
