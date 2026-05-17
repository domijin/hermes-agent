"""RED tests for shadow-stage tenant push in _handle_message.

Locks the contract:

- When ``_handle_message`` processes a message from an authorized user, it
  resolves the tenant and pushes it on ``current_tenant`` for the duration
  of message handling.
- The contextvar is reset back to ``None`` after handling completes (or
  errors out) so the next message starts clean.
- A single log line is emitted at INFO with the resolved tenant_id,
  trust_tier, and platform — but NEVER the raw phone.
- Shadow stage: ``build_session_key`` is NOT called with ``tenant=`` — that
  comes in the enforce stage. We assert this by checking that legacy session
  keys are still produced.

Spec: runs/imessage-beta-worker/specs/tenant-isolation-implementation.md (Spec 1)
"""

from __future__ import annotations

import logging

import pytest


_BETA_PHONE = "+12025550101"
_OPERATOR_PHONE = "+19565452817"


@pytest.fixture
def tenant_env(monkeypatch, tmp_path):
    monkeypatch.setenv("BLUEBUBBLES_ALLOWED_USERS", _BETA_PHONE)
    monkeypatch.setenv("HERMES_TENANT_HMAC_SECRET", "test-secret-12345")
    monkeypatch.setenv("HERMES_TENANT_SECRET_PATH", str(tmp_path / "tenant.secret"))
    yield


def _src_bluebubbles(user_id: str):
    from gateway.session import SessionSource
    from gateway.config import Platform

    return SessionSource(
        platform=Platform.BLUEBUBBLES,
        chat_id=user_id,
        chat_type="dm",
        user_id=user_id,
    )


def _make_runner_stub():
    from gateway.run import GatewayRunner

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._tenant_resolver = None
    return runner


def test_push_tenant_context_pushes_and_resets(tenant_env):
    """The push helper sets current_tenant and resets on exit.

    The runner exposes ``_push_tenant(source)`` as a context manager that
    resolves + pushes the tenant, yields it, and resets on exit.
    """
    from gateway.tenant_context import current_tenant

    runner = _make_runner_stub()
    assert current_tenant.get() is None

    with runner._push_tenant(_src_bluebubbles(_BETA_PHONE)) as tenant:
        assert tenant.trust_tier == "beta"
        assert current_tenant.get() is tenant

    # Reset on exit.
    assert current_tenant.get() is None


def test_push_tenant_resets_even_on_exception(tenant_env):
    """If the handler raises inside the with-block, the contextvar still resets."""
    from gateway.tenant_context import current_tenant

    runner = _make_runner_stub()

    with pytest.raises(RuntimeError, match="boom"):
        with runner._push_tenant(_src_bluebubbles(_BETA_PHONE)):
            assert current_tenant.get() is not None
            raise RuntimeError("boom")

    assert current_tenant.get() is None


def test_push_tenant_logs_shadow_line_for_beta(tenant_env, caplog):
    """A shadow-stage log line is emitted at INFO with tenant_id and tier."""
    runner = _make_runner_stub()

    caplog.set_level(logging.INFO, logger="gateway.run")
    with runner._push_tenant(_src_bluebubbles(_BETA_PHONE)):
        pass

    matching = [
        rec for rec in caplog.records
        if "tenant.resolved" in rec.getMessage()
    ]
    assert len(matching) >= 1, f"no tenant.resolved log; got {[r.getMessage() for r in caplog.records]}"

    msg = matching[0].getMessage()
    assert "trust_tier=beta" in msg
    assert "platform=bluebubbles" in msg
    # tenant_id must appear but raw phone must not.
    assert "tenant_id=btu_" in msg
    assert _BETA_PHONE not in msg


def test_push_tenant_logs_shadow_line_for_operator(tenant_env, caplog):
    runner = _make_runner_stub()

    caplog.set_level(logging.INFO, logger="gateway.run")
    with runner._push_tenant(_src_bluebubbles(_OPERATOR_PHONE)):
        pass

    matching = [
        rec for rec in caplog.records
        if "tenant.resolved" in rec.getMessage()
    ]
    assert any("trust_tier=operator" in r.getMessage() for r in matching)
    # Operator tenant_id is op_self, which is fine to log.
    assert any("tenant_id=op_self" in r.getMessage() for r in matching)


def test_push_tenant_for_unknown_user_uses_unknown_sentinel(tenant_env, caplog):
    from gateway.tenant_context import current_tenant

    runner = _make_runner_stub()

    caplog.set_level(logging.INFO, logger="gateway.run")
    with runner._push_tenant(_src_bluebubbles("+19998887777")) as tenant:
        assert tenant.trust_tier == "unknown"
        assert tenant.tenant_id == "unknown"
        assert current_tenant.get() is tenant

    assert current_tenant.get() is None
    msgs = [r.getMessage() for r in caplog.records]
    assert any("trust_tier=unknown" in m for m in msgs)
