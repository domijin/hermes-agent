"""Tests for hermes-work public-channel UX patches (domi-local-patch).

Covers:
- _slack_ephemeral_intermediate_in_channels / _slack_ephemeral_recipients /
  _slack_approve_send_in_channels config readers (req #2 + req #3).
- _send_ephemeral_to_recipients: chat.postEphemeral per recipient.
- _send_await_send_approval: Block Kit Approve/Deny gating with asyncio.Event.
- _handle_send_approval_action: button-click handler.
- send() integration: routing to ephemeral or approval before chat_postMessage.

See docs/hermes-work-public-channel-spec.md.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Ensure repo root is importable + minimal slack_bolt mock
# ---------------------------------------------------------------------------

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


def _ensure_slack_mock():
    if "slack_bolt" in sys.modules:
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    sys.modules["slack_bolt"] = slack_bolt
    sys.modules["slack_bolt.async_app"] = slack_bolt.async_app
    handler_mod = MagicMock()
    handler_mod.AsyncSocketModeHandler = MagicMock
    sys.modules["slack_bolt.adapter"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode.async_handler"] = handler_mod
    sdk_mod = MagicMock()
    sdk_mod.web = MagicMock()
    sdk_mod.web.async_client = MagicMock()
    sdk_mod.web.async_client.AsyncWebClient = MagicMock
    sys.modules["slack_sdk"] = sdk_mod
    sys.modules["slack_sdk.web"] = sdk_mod.web
    sys.modules["slack_sdk.web.async_client"] = sdk_mod.web.async_client


_ensure_slack_mock()

import gateway.platforms.slack as _slack_mod  # noqa: E402
_slack_mod.SLACK_AVAILABLE = True

from gateway.config import Platform, PlatformConfig  # noqa: E402
from gateway.platforms.slack import SlackAdapter  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_adapter(extra=None):
    """SlackAdapter with mocked internals; `extra` populates config.extra."""
    config = PlatformConfig(enabled=True, token="xoxb-test", extra=extra or {})
    adapter = SlackAdapter(config)
    adapter._app = MagicMock()
    adapter._bot_user_id = "U_BOT"
    adapter._team_clients = {"T1": AsyncMock()}
    adapter._team_bot_user_ids = {"T1": "U_BOT"}
    adapter._channel_team = {"CPUB": "T1", "COTHER": "T1"}
    return adapter


# ===========================================================================
# Config readers
# ===========================================================================

class TestEphemeralIntermediateChannelsReader:
    def test_unset_returns_empty(self, monkeypatch):
        monkeypatch.delenv("SLACK_EPHEMERAL_INTERMEDIATE_IN_CHANNELS", raising=False)
        a = _make_adapter()
        assert a._slack_ephemeral_intermediate_in_channels() == set()

    def test_list_form(self):
        a = _make_adapter({"ephemeral_intermediate_in_channels": ["C1", "C2"]})
        assert a._slack_ephemeral_intermediate_in_channels() == {"C1", "C2"}

    def test_csv_string(self):
        a = _make_adapter({"ephemeral_intermediate_in_channels": "C1,C2 , C3"})
        assert a._slack_ephemeral_intermediate_in_channels() == {"C1", "C2", "C3"}

    def test_env_override_when_unset(self, monkeypatch):
        monkeypatch.setenv("SLACK_EPHEMERAL_INTERMEDIATE_IN_CHANNELS", "CENV")
        a = _make_adapter()
        assert a._slack_ephemeral_intermediate_in_channels() == {"CENV"}


class TestEphemeralRecipientsReader:
    def test_unset_returns_empty_list(self, monkeypatch):
        monkeypatch.delenv("SLACK_EPHEMERAL_RECIPIENTS", raising=False)
        a = _make_adapter()
        assert a._slack_ephemeral_recipients() == []

    def test_list_preserves_order(self):
        a = _make_adapter({"ephemeral_recipients": ["U1", "U2", "U3"]})
        assert a._slack_ephemeral_recipients() == ["U1", "U2", "U3"]

    def test_csv_string(self):
        a = _make_adapter({"ephemeral_recipients": "U1, U2"})
        assert a._slack_ephemeral_recipients() == ["U1", "U2"]


class TestApproveSendChannelsReader:
    def test_unset_empty(self, monkeypatch):
        monkeypatch.delenv("SLACK_APPROVE_SEND_IN_CHANNELS", raising=False)
        a = _make_adapter()
        assert a._slack_approve_send_in_channels() == set()

    def test_list_form(self):
        a = _make_adapter({"approve_send_in_channels": ["CPUB"]})
        assert a._slack_approve_send_in_channels() == {"CPUB"}


# ===========================================================================
# _send_ephemeral_to_recipients
# ===========================================================================

class TestSendEphemeralToRecipients:
    @pytest.mark.asyncio
    async def test_calls_postEphemeral_per_recipient(self):
        a = _make_adapter({"ephemeral_recipients": ["U1", "U2"]})
        a._team_clients["T1"].chat_postEphemeral = AsyncMock(return_value={"ok": True})

        result = await a._send_ephemeral_to_recipients("CPUB", "hello world")

        assert result.success is True
        client = a._team_clients["T1"]
        assert client.chat_postEphemeral.await_count == 2
        users_called = {c.kwargs["user"] for c in client.chat_postEphemeral.await_args_list}
        assert users_called == {"U1", "U2"}
        for c in client.chat_postEphemeral.await_args_list:
            assert c.kwargs["channel"] == "CPUB"
            assert c.kwargs["text"]  # truncate_message can return formatted text

    @pytest.mark.asyncio
    async def test_no_recipients_drops_silently(self):
        a = _make_adapter()  # ephemeral_recipients not set
        a._team_clients["T1"].chat_postEphemeral = AsyncMock()

        result = await a._send_ephemeral_to_recipients("CPUB", "hello")

        assert result.success is True
        assert result.error == "no-recipients-configured"
        a._team_clients["T1"].chat_postEphemeral.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_postEphemeral_exception_continues(self):
        """One failing recipient shouldn't block delivery to the others."""
        a = _make_adapter({"ephemeral_recipients": ["U_FAIL", "U_OK"]})

        async def selective_fail(**kw):
            if kw.get("user") == "U_FAIL":
                raise RuntimeError("Slack 500")
            return {"ok": True}

        a._team_clients["T1"].chat_postEphemeral = AsyncMock(side_effect=selective_fail)
        result = await a._send_ephemeral_to_recipients("CPUB", "msg")
        assert result.success is True
        assert a._team_clients["T1"].chat_postEphemeral.await_count == 2


# ===========================================================================
# _send_await_send_approval + _handle_send_approval_action
# ===========================================================================

class TestSendApprovalRoundtrip:
    @pytest.mark.asyncio
    async def test_approve_button_returns_true(self):
        a = _make_adapter({"ephemeral_recipients": ["U_DOMI"]})
        a._team_clients["T1"].chat_postEphemeral = AsyncMock(return_value={"ok": True})

        async def click_approve_soon():
            # Let _send_await_send_approval post the prompt + register the event.
            await asyncio.sleep(0.05)
            (approval_key,) = list(a._send_approval_events.keys())
            ack = AsyncMock()
            await a._handle_send_approval_action(
                ack=ack,
                body={"user": {"id": "U_DOMI"}},
                action={"action_id": "hermes_send_approve", "value": approval_key},
            )

        async with asyncio.TaskGroup() as tg:  # type: ignore[attr-defined]
            tg.create_task(click_approve_soon())
            t = tg.create_task(
                a._send_await_send_approval("CPUB", "the message", timeout=2.0)
            )
        assert t.result() is True
        # event entry cleaned up
        assert a._send_approval_events == {}
        assert a._send_approval_results == {}

    @pytest.mark.asyncio
    async def test_deny_button_returns_false(self):
        a = _make_adapter({"ephemeral_recipients": ["U_DOMI"]})
        a._team_clients["T1"].chat_postEphemeral = AsyncMock(return_value={"ok": True})

        async def click_deny_soon():
            await asyncio.sleep(0.05)
            (approval_key,) = list(a._send_approval_events.keys())
            await a._handle_send_approval_action(
                ack=AsyncMock(),
                body={"user": {"id": "U_DOMI"}},
                action={"action_id": "hermes_send_deny", "value": approval_key},
            )

        async with asyncio.TaskGroup() as tg:  # type: ignore[attr-defined]
            tg.create_task(click_deny_soon())
            t = tg.create_task(
                a._send_await_send_approval("CPUB", "the message", timeout=2.0)
            )
        assert t.result() is False

    @pytest.mark.asyncio
    async def test_unauthorized_click_ignored(self):
        a = _make_adapter({"ephemeral_recipients": ["U_DOMI"]})
        a._team_clients["T1"].chat_postEphemeral = AsyncMock(return_value={"ok": True})

        async def click_by_stranger():
            await asyncio.sleep(0.05)
            (approval_key,) = list(a._send_approval_events.keys())
            await a._handle_send_approval_action(
                ack=AsyncMock(),
                body={"user": {"id": "U_STRANGER"}},
                action={"action_id": "hermes_send_approve", "value": approval_key},
            )

        async with asyncio.TaskGroup() as tg:  # type: ignore[attr-defined]
            tg.create_task(click_by_stranger())
            t = tg.create_task(
                a._send_await_send_approval("CPUB", "msg", timeout=0.5)
            )
        # Click by stranger ignored — must time out → False
        assert t.result() is False

    @pytest.mark.asyncio
    async def test_no_recipients_denies_by_default(self):
        a = _make_adapter()  # no ephemeral_recipients
        a._team_clients["T1"].chat_postEphemeral = AsyncMock()
        result = await a._send_await_send_approval("CPUB", "msg", timeout=0.1)
        assert result is False
        a._team_clients["T1"].chat_postEphemeral.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_timeout_denies(self):
        a = _make_adapter({"ephemeral_recipients": ["U_DOMI"]})
        a._team_clients["T1"].chat_postEphemeral = AsyncMock(return_value={"ok": True})
        result = await a._send_await_send_approval("CPUB", "msg", timeout=0.1)
        assert result is False

    @pytest.mark.asyncio
    async def test_approve_click_replaces_buttons_with_confirmation(self):
        """After Approve click, the ephemeral prompt is replaced via
        respond(replace_original=True). The buttons go away; the operator
        sees '✅ Approved by ...' instead of lingering Approve/Deny.

        Regression: 2026-05-20 screenshot — buttons persisted after click."""
        a = _make_adapter({"ephemeral_recipients": ["U_DOMI"]})
        a._team_clients["T1"].chat_postEphemeral = AsyncMock(return_value={"ok": True})
        respond = AsyncMock()
        approval_key = "test-key"
        a._send_approval_events[approval_key] = asyncio.Event()

        await a._handle_send_approval_action(
            ack=AsyncMock(),
            body={
                "user": {"id": "U_DOMI", "name": "domi"},
                "message": {"blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "draft answer"}},
                ]},
            },
            action={"action_id": "hermes_send_approve", "value": approval_key},
            respond=respond,
        )

        respond.assert_awaited_once()
        kwargs = respond.await_args.kwargs
        assert kwargs.get("replace_original") is True
        assert "Approved by domi" in kwargs.get("text", "")
        # Original draft text is preserved in the updated blocks so the
        # operator can still see what they approved.
        blocks = kwargs.get("blocks", [])
        section_texts = [b["text"]["text"] for b in blocks if b.get("type") == "section"]
        assert any("draft answer" in t for t in section_texts)

    @pytest.mark.asyncio
    async def test_deny_click_replaces_buttons_with_deny_confirmation(self):
        """Symmetric: Deny click also replaces the buttons with a Deny notice."""
        a = _make_adapter({"ephemeral_recipients": ["U_DOMI"]})
        a._team_clients["T1"].chat_postEphemeral = AsyncMock(return_value={"ok": True})
        respond = AsyncMock()
        approval_key = "test-key"
        a._send_approval_events[approval_key] = asyncio.Event()

        await a._handle_send_approval_action(
            ack=AsyncMock(),
            body={
                "user": {"id": "U_DOMI", "name": "domi"},
                "message": {"blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": "draft answer"}},
                ]},
            },
            action={"action_id": "hermes_send_deny", "value": approval_key},
            respond=respond,
        )

        respond.assert_awaited_once()
        kwargs = respond.await_args.kwargs
        assert kwargs.get("replace_original") is True
        assert "Denied by domi" in kwargs.get("text", "")

    @pytest.mark.asyncio
    async def test_action_handler_works_without_respond(self):
        """Handler called without ``respond`` (unit-test path) still sets the
        event correctly — the prompt just isn't updated. Verifies the
        respond=None default preserves backward-compat with existing tests."""
        a = _make_adapter({"ephemeral_recipients": ["U_DOMI"]})
        approval_key = "test-key"
        event = asyncio.Event()
        a._send_approval_events[approval_key] = event

        await a._handle_send_approval_action(
            ack=AsyncMock(),
            body={"user": {"id": "U_DOMI"}, "message": {"blocks": []}},
            action={"action_id": "hermes_send_approve", "value": approval_key},
            # respond omitted intentionally
        )

        assert event.is_set()
        assert a._send_approval_results.get(approval_key) is True


# ===========================================================================
# send() integration: top-of-send routing
# ===========================================================================

class TestSendRouting:
    @pytest.mark.asyncio
    async def test_ephemeral_channel_does_not_post_public(self):
        a = _make_adapter({
            "ephemeral_intermediate_in_channels": ["CPUB"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock()

        # send() needs _pop_slash_context to return None — make sure it does.
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("CPUB", "intermediate progress")

        assert result.success is True
        client.chat_postMessage.assert_not_awaited()
        client.chat_postEphemeral.assert_awaited_once()
        assert client.chat_postEphemeral.await_args.kwargs["user"] == "U_DOMI"

    @pytest.mark.asyncio
    async def test_non_target_channel_falls_through_to_public(self):
        a = _make_adapter({
            "ephemeral_intermediate_in_channels": ["CPUB"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("COTHER", "public message")

        assert result.success is True
        client.chat_postMessage.assert_awaited()
        client.chat_postEphemeral.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approve_channel_final_send_blocks_until_approved_then_posts_public(self):
        """Final send (metadata['is_final']=True) in approve channel:
        approval prompt → on Approve → public post.

        2026-05-20 (Option D, spec §6): approval gate now fires ONLY on
        the canonical turn-final send (set by base.py at the agent-response
        site). Intermediate sends do not prompt for approval — see
        test_approve_channel_intermediate_send_no_approval below.
        """
        a = _make_adapter({
            "approve_send_in_channels": ["CPUB"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        async def click_approve():
            for _ in range(100):
                if a._send_approval_events:
                    break
                await asyncio.sleep(0.01)
            (approval_key,) = list(a._send_approval_events.keys())
            await a._handle_send_approval_action(
                ack=AsyncMock(),
                body={"user": {"id": "U_DOMI"}},
                action={"action_id": "hermes_send_approve", "value": approval_key},
            )

        async with asyncio.TaskGroup() as tg:  # type: ignore[attr-defined]
            tg.create_task(click_approve())
            t = tg.create_task(a.send("CPUB", "draft answer", metadata={"is_final": True}))
        assert t.result().success is True
        client.chat_postEphemeral.assert_awaited()
        client.chat_postMessage.assert_awaited()

    @pytest.mark.asyncio
    async def test_approve_channel_final_denied_returns_success_no_public_post(self):
        """Final send + Deny click: success-with-error, no public post."""
        a = _make_adapter({
            "approve_send_in_channels": ["CPUB"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        async def click_deny():
            for _ in range(100):
                if a._send_approval_events:
                    break
                await asyncio.sleep(0.01)
            (approval_key,) = list(a._send_approval_events.keys())
            await a._handle_send_approval_action(
                ack=AsyncMock(),
                body={"user": {"id": "U_DOMI"}},
                action={"action_id": "hermes_send_deny", "value": approval_key},
            )

        async with asyncio.TaskGroup() as tg:  # type: ignore[attr-defined]
            tg.create_task(click_deny())
            t = tg.create_task(a.send("CPUB", "draft answer", metadata={"is_final": True}))
        result = t.result()
        assert result.success is True
        assert result.error == "user-denied"
        client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_approve_channel_intermediate_send_no_approval(self):
        """Intermediate send (no is_final / is_final=False) in an approve channel
        with NO ephemeral routing configured: posts publicly with NO approval
        prompt. Option D requires the gate to fire ONLY on the final turn send,
        not on every adapter.send (tool-progress, footer, etc.)."""
        a = _make_adapter({
            "approve_send_in_channels": ["CPUB"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        # No metadata, no is_final → goes straight to public (legacy path).
        result = await a.send("CPUB", "tool progress…")

        assert result.success is True
        client.chat_postEphemeral.assert_not_awaited()
        client.chat_postMessage.assert_awaited()

    @pytest.mark.asyncio
    async def test_approve_channel_intermediate_in_both_lists_routes_ephemeral(self):
        """The typical Option D channel config: chat in BOTH ephemeral_intermediate
        AND approve_send. Intermediate (no is_final) → ephemeral. NO approval
        prompt, NO public post."""
        a = _make_adapter({
            "ephemeral_intermediate_in_channels": ["CPUB"],
            "approve_send_in_channels": ["CPUB"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("CPUB", "tool progress…")

        assert result.success is True
        client.chat_postEphemeral.assert_awaited()  # routed ephemerally
        client.chat_postMessage.assert_not_awaited()  # no public post

    @pytest.mark.asyncio
    async def test_approve_channel_final_approved_bypasses_ephemeral_list(self):
        """Final send + chat in BOTH lists + Approve → posts publicly, NOT
        wrapped as ephemeral. This is the Option D win: after operator
        approval, the answer lands publicly so the channel sees it."""
        a = _make_adapter({
            "ephemeral_intermediate_in_channels": ["CPUB"],
            "approve_send_in_channels": ["CPUB"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        async def click_approve():
            for _ in range(100):
                if a._send_approval_events:
                    break
                await asyncio.sleep(0.01)
            (approval_key,) = list(a._send_approval_events.keys())
            await a._handle_send_approval_action(
                ack=AsyncMock(),
                body={"user": {"id": "U_DOMI"}},
                action={"action_id": "hermes_send_approve", "value": approval_key},
            )

        async with asyncio.TaskGroup() as tg:  # type: ignore[attr-defined]
            tg.create_task(click_approve())
            t = tg.create_task(a.send("CPUB", "the final answer", metadata={"is_final": True}))
        result = t.result()
        assert result.success is True
        # Approval prompt was ephemeral; final approved post is public (NOT
        # routed via chat.postEphemeral despite the channel being in the
        # ephemeral_intermediate list).
        client.chat_postMessage.assert_awaited()


# ===========================================================================
# default_channel_policy — Option A: close the public-post fall-through hole.
# Channels in NONE of the explicit allowlists previously fell through to a
# public chat_postMessage. default_channel_policy governs that fall-through.
# See docs/hermes-work-public-channel-spec.md (Option A).
# ===========================================================================

class TestDefaultChannelPolicyReader:
    def test_unset_defaults_to_public_backcompat(self, monkeypatch):
        """Unset → 'public' so existing operator configs behave identically."""
        monkeypatch.delenv("SLACK_DEFAULT_CHANNEL_POLICY", raising=False)
        a = _make_adapter()
        assert a._slack_default_channel_policy() == "public"

    def test_explicit_ephemeral(self):
        a = _make_adapter({"default_channel_policy": "ephemeral"})
        assert a._slack_default_channel_policy() == "ephemeral"

    def test_explicit_ephemeral_with_approval(self):
        a = _make_adapter({"default_channel_policy": "ephemeral_with_approval"})
        assert a._slack_default_channel_policy() == "ephemeral_with_approval"

    def test_whitespace_and_case_normalized(self):
        a = _make_adapter({"default_channel_policy": "  EPHEMERAL  "})
        assert a._slack_default_channel_policy() == "ephemeral"

    def test_env_override_when_unset(self, monkeypatch):
        monkeypatch.setenv("SLACK_DEFAULT_CHANNEL_POLICY", "ephemeral_with_approval")
        a = _make_adapter()
        assert a._slack_default_channel_policy() == "ephemeral_with_approval"

    def test_invalid_value_coerced_to_public(self):
        """Unknown policy strings fall back to the back-compat 'public' default
        rather than raising — predictable, never crashes a live send."""
        a = _make_adapter({"default_channel_policy": "nonsense"})
        assert a._slack_default_channel_policy() == "public"


class TestPublicChannelsReader:
    def test_unset_empty(self, monkeypatch):
        monkeypatch.delenv("SLACK_PUBLIC_CHANNELS", raising=False)
        a = _make_adapter()
        assert a._slack_public_channels() == set()

    def test_list_form(self):
        a = _make_adapter({"public_channels": ["CPUB", "C2"]})
        assert a._slack_public_channels() == {"CPUB", "C2"}

    def test_csv_string(self):
        a = _make_adapter({"public_channels": "CPUB, C2"})
        assert a._slack_public_channels() == {"CPUB", "C2"}

    def test_env_override_when_unset(self, monkeypatch):
        monkeypatch.setenv("SLACK_PUBLIC_CHANNELS", "CENV")
        a = _make_adapter()
        assert a._slack_public_channels() == {"CENV"}


class TestChannelPolicyPrecedence:
    """_channel_policy(chat_id) resolves the effective policy via the locked
    precedence: approve_send > ephemeral_intermediate > public_channels >
    default_channel_policy."""

    def test_approve_send_wins(self):
        a = _make_adapter({
            "approve_send_in_channels": ["CPUB"],
            "ephemeral_intermediate_in_channels": ["CPUB"],
            "public_channels": ["CPUB"],
            "default_channel_policy": "ephemeral",
        })
        assert a._channel_policy("CPUB") == "ephemeral_with_approval"

    def test_ephemeral_intermediate_second(self):
        a = _make_adapter({
            "ephemeral_intermediate_in_channels": ["CPUB"],
            "public_channels": ["CPUB"],
            "default_channel_policy": "public",
        })
        assert a._channel_policy("CPUB") == "ephemeral"

    def test_public_channels_third(self):
        a = _make_adapter({
            "public_channels": ["CPUB"],
            "default_channel_policy": "ephemeral_with_approval",
        })
        assert a._channel_policy("CPUB") == "public"

    def test_unlisted_falls_to_default(self):
        a = _make_adapter({"default_channel_policy": "ephemeral_with_approval"})
        assert a._channel_policy("COTHER") == "ephemeral_with_approval"

    def test_unlisted_default_unset_is_public(self, monkeypatch):
        monkeypatch.delenv("SLACK_DEFAULT_CHANNEL_POLICY", raising=False)
        a = _make_adapter()
        assert a._channel_policy("COTHER") == "public"


class TestDefaultPolicyFallThroughRouting:
    """send() to a channel in NO explicit allowlist routes per
    default_channel_policy (the four Option A fall-through cases)."""

    @pytest.mark.asyncio
    async def test_default_unset_unlisted_posts_public_backcompat(self, monkeypatch):
        """BACK-COMPAT: with no default_channel_policy set, an unlisted channel
        posts publicly — byte-identical to pre-Option-A behavior."""
        monkeypatch.delenv("SLACK_DEFAULT_CHANNEL_POLICY", raising=False)
        a = _make_adapter({
            "ephemeral_intermediate_in_channels": ["CPUB"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("COTHER", "hello unlisted")

        assert result.success is True
        client.chat_postMessage.assert_awaited()
        client.chat_postEphemeral.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_public_unlisted_posts_public(self):
        a = _make_adapter({
            "default_channel_policy": "public",
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("COTHER", "hello")

        assert result.success is True
        client.chat_postMessage.assert_awaited()
        client.chat_postEphemeral.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_ephemeral_unlisted_posts_ephemeral(self):
        a = _make_adapter({
            "default_channel_policy": "ephemeral",
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock()
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("COTHER", "hello")

        assert result.success is True
        client.chat_postEphemeral.assert_awaited()
        assert client.chat_postEphemeral.await_args.kwargs["user"] == "U_DOMI"
        client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_approval_final_send_prompts_then_posts_public(self):
        a = _make_adapter({
            "default_channel_policy": "ephemeral_with_approval",
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        async def click_approve():
            for _ in range(100):
                if a._send_approval_events:
                    break
                await asyncio.sleep(0.01)
            (approval_key,) = list(a._send_approval_events.keys())
            await a._handle_send_approval_action(
                ack=AsyncMock(),
                body={"user": {"id": "U_DOMI"}},
                action={"action_id": "hermes_send_approve", "value": approval_key},
            )

        async with asyncio.TaskGroup() as tg:  # type: ignore[attr-defined]
            tg.create_task(click_approve())
            t = tg.create_task(a.send("COTHER", "draft answer", metadata={"is_final": True}))
        assert t.result().success is True
        client.chat_postEphemeral.assert_awaited()  # approval prompt
        client.chat_postMessage.assert_awaited()    # approved → public

    @pytest.mark.asyncio
    async def test_default_approval_final_denied_no_public(self):
        a = _make_adapter({
            "default_channel_policy": "ephemeral_with_approval",
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        async def click_deny():
            for _ in range(100):
                if a._send_approval_events:
                    break
                await asyncio.sleep(0.01)
            (approval_key,) = list(a._send_approval_events.keys())
            await a._handle_send_approval_action(
                ack=AsyncMock(),
                body={"user": {"id": "U_DOMI"}},
                action={"action_id": "hermes_send_deny", "value": approval_key},
            )

        async with asyncio.TaskGroup() as tg:  # type: ignore[attr-defined]
            tg.create_task(click_deny())
            t = tg.create_task(a.send("COTHER", "draft answer", metadata={"is_final": True}))
        result = t.result()
        assert result.success is True
        assert result.error == "user-denied"
        client.chat_postMessage.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_default_approval_intermediate_send_routes_ephemeral_not_public(self):
        """SAFE-BY-DEFAULT: under default=ephemeral_with_approval, a non-final
        (intermediate) send to an unlisted channel must NOT leak publicly — it
        routes ephemerally and shows no approval prompt."""
        a = _make_adapter({
            "default_channel_policy": "ephemeral_with_approval",
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("COTHER", "tool progress…")  # no is_final

        assert result.success is True
        client.chat_postEphemeral.assert_awaited()
        client.chat_postMessage.assert_not_awaited()
        assert not a._send_approval_events  # no approval prompt was raised

    @pytest.mark.asyncio
    async def test_public_channels_overrides_safe_default(self):
        """An explicit public_channels entry opts out of a safe default: the
        channel posts publicly even when default_channel_policy would gate it."""
        a = _make_adapter({
            "default_channel_policy": "ephemeral_with_approval",
            "public_channels": ["COTHER"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("COTHER", "public override", metadata={"is_final": True})

        assert result.success is True
        client.chat_postMessage.assert_awaited()
        client.chat_postEphemeral.assert_not_awaited()


# ===========================================================================
# Cron deliveries bypass ALL policy gates (metadata['is_cron']=True).
# cron/scheduler.py tags auto-deliveries so a scheduled job's output reaches
# the channel publicly — there is no operator to click an Approve prompt.
# ===========================================================================

class TestCronDeliveryBypassesPolicy:
    @pytest.mark.asyncio
    async def test_cron_posts_public_under_approval_default(self):
        """Unlisted channel + default=ephemeral_with_approval + is_cron → public,
        no ephemeral, no approval prompt."""
        a = _make_adapter({
            "default_channel_policy": "ephemeral_with_approval",
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("COTHER", "cron summary", metadata={"is_cron": True, "is_final": True})

        assert result.success is True
        client.chat_postMessage.assert_awaited()
        client.chat_postEphemeral.assert_not_awaited()
        assert not a._send_approval_events  # no approval prompt raised

    @pytest.mark.asyncio
    async def test_cron_bypasses_approve_send_channel(self):
        """is_cron beats approve_send_in_channels: a cron final send posts
        publicly with NO approval gate (even though the channel is gated)."""
        a = _make_adapter({
            "approve_send_in_channels": ["CPUB"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("CPUB", "cron final", metadata={"is_cron": True, "is_final": True})

        assert result.success is True
        client.chat_postMessage.assert_awaited()
        client.chat_postEphemeral.assert_not_awaited()
        assert not a._send_approval_events

    @pytest.mark.asyncio
    async def test_cron_bypasses_ephemeral_intermediate_channel(self):
        """is_cron beats ephemeral_intermediate_in_channels: posts publicly,
        not ephemerally."""
        a = _make_adapter({
            "ephemeral_intermediate_in_channels": ["CPUB"],
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock()
        client.chat_postMessage = AsyncMock(return_value={"ts": "1.2"})
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("CPUB", "cron progress", metadata={"is_cron": True})

        assert result.success is True
        client.chat_postMessage.assert_awaited()
        client.chat_postEphemeral.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_cron_still_gated(self):
        """Sanity: the SAME channel without is_cron is still gated (the bypass
        is strictly opt-in via metadata)."""
        a = _make_adapter({
            "default_channel_policy": "ephemeral",
            "ephemeral_recipients": ["U_DOMI"],
        })
        client = a._team_clients["T1"]
        client.chat_postEphemeral = AsyncMock(return_value={"ok": True})
        client.chat_postMessage = AsyncMock()
        a._pop_slash_context = MagicMock(return_value=None)

        result = await a.send("COTHER", "normal message")  # no is_cron

        assert result.success is True
        client.chat_postEphemeral.assert_awaited()
        client.chat_postMessage.assert_not_awaited()
