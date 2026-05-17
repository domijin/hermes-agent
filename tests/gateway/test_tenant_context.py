"""RED tests for gateway.tenant_context — Phase 1 of Hermes-lite tenant isolation.

These tests are expected to fail until `gateway/tenant_context.py` is implemented.
They lock the contract for the canonical TenantContext dataclass and the
per-task contextvar that threads tenant identity through async handlers.

Spec: runs/imessage-beta-worker/specs/tenant-isolation-implementation.md (Spec 1)
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest


def test_tenant_context_is_frozen():
    """TenantContext must be a frozen, hashable dataclass.

    Frozen-ness prevents accidental mutation of tenant identity mid-request,
    which is the entire point of this object. Hashability lets us use it as a
    contextvar value or dict key when needed.
    """
    from gateway.tenant_context import TenantContext

    ctx = TenantContext(
        tenant_id="btu_test01",
        platform="bluebubbles",
        external_user_ref="+12025550101",
        display_alias="User A",
        trust_tier="beta",
        privacy_mode="strict",
    )

    # Frozen — assignment must raise.
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.tenant_id = "btu_other"  # type: ignore[misc]

    # Hashable — must not raise.
    hash(ctx)

    # Same field values → equal.
    same = TenantContext(
        tenant_id="btu_test01",
        platform="bluebubbles",
        external_user_ref="+12025550101",
        display_alias="User A",
        trust_tier="beta",
        privacy_mode="strict",
    )
    assert ctx == same


def test_unknown_tenant_default():
    """TenantContext.unknown() is the safe fail-closed sentinel."""
    from gateway.tenant_context import TenantContext

    unknown = TenantContext.unknown()
    assert unknown.tenant_id == "unknown"
    assert unknown.trust_tier == "unknown"
    assert unknown.privacy_mode == "strict"


def test_contextvar_push_pop():
    """current_tenant.set() pushes a value; reset restores prior state."""
    from gateway.tenant_context import TenantContext, current_tenant

    # Default state — no tenant in this fresh test context.
    assert current_tenant.get() is None

    ctx = TenantContext(
        tenant_id="btu_push01",
        platform="bluebubbles",
        external_user_ref="+12025550102",
        display_alias="User B",
        trust_tier="beta",
        privacy_mode="strict",
    )
    token = current_tenant.set(ctx)
    try:
        assert current_tenant.get() is ctx
    finally:
        current_tenant.reset(token)

    assert current_tenant.get() is None


def test_concurrent_tasks_have_isolated_tenant():
    """Two concurrent asyncio tasks must see their own TenantContext.

    This is the core invariant — contextvars are task-local, so one user's
    tenant never bleeds into another user's concurrent request.
    """
    from gateway.tenant_context import TenantContext, current_tenant

    def make(name: str) -> TenantContext:
        return TenantContext(
            tenant_id=f"btu_{name}",
            platform="bluebubbles",
            external_user_ref=f"+1202555010{name}",
            display_alias=f"User {name}",
            trust_tier="beta",
            privacy_mode="strict",
        )

    async def worker(name: str, barrier: asyncio.Event) -> str:
        current_tenant.set(make(name))
        # Wait until the other task has also set its tenant before reading,
        # so any cross-task leakage would be observable.
        await barrier.wait()
        # Yield a few times to give the scheduler a chance to interleave.
        for _ in range(3):
            await asyncio.sleep(0)
        ctx = current_tenant.get()
        assert ctx is not None
        return ctx.tenant_id

    async def driver():
        barrier = asyncio.Event()
        t1 = asyncio.create_task(worker("A1", barrier))
        t2 = asyncio.create_task(worker("B2", barrier))
        # Let both tasks reach the barrier.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        barrier.set()
        return await asyncio.gather(t1, t2)

    results = asyncio.run(driver())
    assert results == ["btu_A1", "btu_B2"]
