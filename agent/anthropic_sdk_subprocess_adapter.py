"""Anthropic SDK subprocess adapter — routes Hermes Claude calls through the
real ``claude`` CLI via ``claude_agent_sdk.query`` for first-party subscription
billing.

Problem
-------
Routing Hermes through an HTTP proxy with an OAuth access token bills against
the prepaid "extra usage" bucket, not the user's Claude Pro/Max subscription.
The subscription quota is only honoured when calls pass through the native
``claude`` CLI subprocess (``claude-agent-sdk``).

This module provides a duck-type shim that mirrors the surface of
``anthropic.Anthropic`` that Hermes uses:

    client.messages.create(
        model=..., messages=..., system=..., tools=..., max_tokens=..., **kw
    )

returning a ``Message``-like object with ``.content``, ``.stop_reason``,
``.usage``, ``.model``, and ``.role``.

PR-B integration point (run_agent.py:1310-1346)
------------------------------------------------
In ``run_agent.py``, the ``_is_anthropic_oauth`` and ``_is_native_anthropic``
detection block builds the Anthropic client.  PR-B should:

1. Import ``build_anthropic_sdk_client`` and ``ProfilePoolExhausted`` from
   this module.
2. When ``_is_anthropic_oauth`` is True AND at least one profile is
   configured under ``hermes_config.get("anthropic_profiles")``, call::

       client = build_anthropic_sdk_client(
           profiles=hermes_config["anthropic_profiles"],
           mcp_servers=hermes_config.get("mcp_servers", {}),
           model=resolved_model,
       )

   and assign it as the Anthropic client.
3. Wrap the outer ``messages.create`` call in a ``try/except
   ProfilePoolExhausted`` that falls back to the existing HTTP OAuth client
   (the ``anthropic.Anthropic(auth_token=...)`` path in run_agent.py:1330+).

No other run_agent.py changes are needed for PR-B.

Components
----------
- ``AuthProfilePool``       — holds N profiles, selects least-used available.
- ``_flatten_messages_to_prompt`` — converts hermes messages+system to prompt.
- ``_collect_to_anthropic_message`` — synthesises a Message-like from SDK events.
- ``build_anthropic_sdk_client``   — factory; returns the client shim.
- ``ProfilePoolExhausted``         — raised when no profile can serve a request.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy SDK import — guarded so the module imports for unit tests even when
# claude_agent_sdk is not installed.  Tests monkeypatch/stub ``query``.
# ---------------------------------------------------------------------------

_claude_agent_sdk: Any = ...  # sentinel — None means "tried and missing"


def _get_claude_agent_sdk():
    """Return the ``claude_agent_sdk`` module, importing lazily.  None if not installed."""
    global _claude_agent_sdk
    if _claude_agent_sdk is ...:
        try:
            import claude_agent_sdk as _sdk
            _claude_agent_sdk = _sdk
        except ImportError:
            _claude_agent_sdk = None
    return _claude_agent_sdk


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProfilePoolExhausted(Exception):
    """Raised by the shim when no profile can serve the request.

    The caller (run_agent.py PR-B) should catch this and fall back to the
    existing HTTP OAuth client path.
    """


# ---------------------------------------------------------------------------
# AuthProfilePool
# ---------------------------------------------------------------------------


@dataclass
class _ProfileEntry:
    """Internal state for one Claude CLI profile."""
    label: str
    token: str                          # sk-ant-oat01-* OAuth setup-token
    config_dir: str                     # per-profile isolation dir (CLAUDE_CONFIG_DIR)
    priority: int
    status: str = "available"          # "available" | "cooldown" | "terminal"
    request_count: int = 0
    cooldown_until: Optional[float] = None


class AuthProfilePool:
    """Thread-safe pool of Claude CLI authentication profiles.

    Each profile maps to a ``~/.claude``-style config directory that
    ``claude_agent_sdk`` can use to run authenticated requests via the real
    ``claude`` CLI subprocess.

    Selection policy
    ----------------
    ``select()`` returns the AVAILABLE profile with the lowest ``request_count``
    (least-used first).  When counts are equal, lower ``priority`` (numerically
    smaller) wins.  Returns ``None`` when no AVAILABLE profile exists.

    Usage
    -----
    ::

        pool = AuthProfilePool(profiles=[
            {"label": "alice", "config_dir": "/home/alice/.claude", "priority": 0},
            {"label": "bob",   "config_dir": "/home/bob/.claude",   "priority": 1},
        ])
        p = pool.select()
        if p is None:
            raise ProfilePoolExhausted("no available profile")
        try:
            ...
            pool.record_success(p)
        except SomeRateLimitError as e:
            pool.handle_cooldown(p, seconds=60)
        except TokenExpiredError as e:
            pool.handle_terminal(p, reason="token expired")
    """

    def __init__(self, profiles: List[Dict[str, Any]]):
        self._lock = threading.Lock()
        self._entries: List[_ProfileEntry] = []
        for p in profiles:
            label = p["label"]
            # config_dir is optional; default to a per-label scratch path so
            # each profile stays isolated even when config_dir is omitted.
            config_dir = p.get("config_dir") or f"/tmp/hermes-claude-{label}"
            self._entries.append(_ProfileEntry(
                label=label,
                token=p.get("token", ""),
                config_dir=config_dir,
                priority=int(p.get("priority", 0)),
                status=p.get("status", "available"),
                request_count=int(p.get("request_count", 0)),
                cooldown_until=p.get("cooldown_until"),
            ))

    def _promote_cooldowns(self) -> None:
        """Expire cooldowns that have passed (caller holds lock)."""
        now = time.monotonic()
        for e in self._entries:
            if e.status == "cooldown" and e.cooldown_until is not None and now >= e.cooldown_until:
                e.status = "available"
                e.cooldown_until = None
                logger.debug("AuthProfilePool: profile %r cooldown expired, marking available", e.label)

    def select(self) -> Optional[_ProfileEntry]:
        """Return the least-used available profile, or None."""
        with self._lock:
            self._promote_cooldowns()
            candidates = [e for e in self._entries if e.status == "available"]
            if not candidates:
                return None
            # Sort by (request_count ASC, priority ASC) — stable, lowest count wins
            candidates.sort(key=lambda e: (e.request_count, e.priority))
            return candidates[0]

    def record_success(self, profile: _ProfileEntry) -> None:
        """Increment the request count for a profile after a successful call."""
        with self._lock:
            profile.request_count += 1
            logger.debug(
                "AuthProfilePool: profile %r succeeded (count=%d)",
                profile.label, profile.request_count,
            )

    def handle_cooldown(self, profile: _ProfileEntry, seconds: float) -> None:
        """Mark a profile as cooling down for ``seconds`` seconds."""
        with self._lock:
            profile.status = "cooldown"
            profile.cooldown_until = time.monotonic() + seconds
            logger.debug(
                "AuthProfilePool: profile %r cooling down for %.0fs",
                profile.label, seconds,
            )

    def handle_terminal(self, profile: _ProfileEntry, reason: str) -> None:
        """Permanently disable a profile (e.g. expired token, invalid key).

        SECURITY: only the ``label`` is logged, not the config_dir or any
        credential values.
        """
        with self._lock:
            profile.status = "terminal"
            logger.warning(
                "AuthProfilePool: profile %r marked terminal — %s",
                profile.label, reason,
            )

    def available_count(self) -> int:
        """Return number of currently available profiles."""
        with self._lock:
            self._promote_cooldowns()
            return sum(1 for e in self._entries if e.status == "available")


# ---------------------------------------------------------------------------
# Message shim
# ---------------------------------------------------------------------------


class _UsageShim:
    """Minimal usage object returned by the shim."""

    def __init__(self, input_tokens: int = 0, output_tokens: int = 0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens

    def __repr__(self):
        return f"_UsageShim(input_tokens={self.input_tokens}, output_tokens={self.output_tokens})"


class _MessageShim:
    """Duck-type replacement for ``anthropic.types.Message``."""

    def __init__(
        self,
        content: List[Dict[str, Any]],
        stop_reason: str,
        usage: _UsageShim,
        model: str,
        role: str = "assistant",
    ):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage
        self.model = model
        self.role = role

    def __repr__(self):
        return (
            f"_MessageShim(role={self.role!r}, stop_reason={self.stop_reason!r}, "
            f"content_blocks={len(self.content)})"
        )


# ---------------------------------------------------------------------------
# Helper: flatten hermes messages to a prompt string
# ---------------------------------------------------------------------------


def _flatten_messages_to_prompt(
    messages: List[Dict[str, Any]],
    system: Any,
) -> str:
    """Convert hermes messages list + system into a single string prompt.

    ``system`` may be:
    - a plain ``str``
    - a list of content blocks (``[{"type": "text", "text": ...}, ...]``) —
      the list form MUST NOT raise; text is extracted and joined.
    - ``None`` or empty — omitted.

    Each message is rendered as ``<ROLE>: <text>`` with a blank line between
    them.  This is intentionally simple — ``claude_agent_sdk.query()`` runs
    the full agent loop from the assembled prompt.
    """
    parts: List[str] = []

    # ── system prompt ──────────────────────────────────────────────────────
    if system:
        if isinstance(system, list):
            # Content-block list — extract text fields, skip non-text blocks
            text_parts = []
            for block in system:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
            system_text = "\n".join(text_parts).strip()
        else:
            system_text = str(system).strip()
        if system_text:
            parts.append(f"System: {system_text}")

    # ── conversation messages ──────────────────────────────────────────────
    for msg in (messages or []):
        role = msg.get("role", "user").capitalize()
        content = msg.get("content", "")
        if isinstance(content, list):
            # Content-block list (vision, tool-use, etc.) — flatten text blocks
            text_parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(str(block.get("text", "")))
            content_text = " ".join(text_parts).strip()
        else:
            content_text = str(content).strip()
        if content_text:
            parts.append(f"{role}: {content_text}")

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Helper: collect SDK event stream into a Message shim
# ---------------------------------------------------------------------------

async def _collect_to_anthropic_message_async(
    events_iter: Any,
    model: str,
) -> _MessageShim:
    """Consume an async iterable of SDK events and synthesise a Message shim.

    Mapping:
    - ``AssistantMessage`` / ``ResultMessage`` / ``TextEvent`` → assistant text
      content block.
    - Final / result event → ``stop_reason`` and ``usage``.

    In mode B (``allowed_tools`` set), tool roundtrips happen inside the SDK;
    we only see the final assistant text.  Multiple text events are collapsed
    into a single content block.
    """
    text_parts: List[str] = []
    stop_reason = "end_turn"
    input_tokens = 0
    output_tokens = 0

    async for event in events_iter:
        # Events are SDK-specific dataclasses; we duck-type on attribute names
        # so the module works with both the real SDK and stub replacements.
        etype = type(event).__name__

        if etype in ("AssistantMessage",):
            # Some SDK versions expose a .message with .content blocks
            msg = getattr(event, "message", None)
            if msg is not None:
                for block in (getattr(msg, "content", None) or []):
                    if hasattr(block, "text"):
                        text_parts.append(block.text)
                    elif isinstance(block, dict) and block.get("type") == "text":
                        text_parts.append(block["text"])
            else:
                # Fallback: event itself may have .text
                text = getattr(event, "text", None)
                if text:
                    text_parts.append(text)

        elif etype in ("ResultMessage", "Result"):
            # Terminal event — carry stop_reason and usage if available
            text = getattr(event, "result", None) or getattr(event, "text", None)
            if text:
                text_parts.append(text)
            sr = getattr(event, "stop_reason", None)
            if sr:
                stop_reason = sr
            usage = getattr(event, "usage", None)
            if usage is not None:
                input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
                output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

        elif etype in ("TextEvent", "ContentBlockDeltaEvent"):
            text = getattr(event, "text", None) or getattr(event, "delta", None)
            if isinstance(text, str):
                text_parts.append(text)
            elif hasattr(text, "text"):
                text_parts.append(text.text)

        elif etype == "SystemPromptEvent":
            pass  # system prompt echo — ignore

        else:
            # Unknown event type — check for a generic .text attribute
            text = getattr(event, "text", None)
            if isinstance(text, str) and text:
                text_parts.append(text)

    full_text = "".join(text_parts).strip()
    content_blocks: List[Dict[str, Any]] = (
        [{"type": "text", "text": full_text}] if full_text else []
    )

    return _MessageShim(
        content=content_blocks,
        stop_reason=stop_reason,
        usage=_UsageShim(input_tokens=input_tokens, output_tokens=output_tokens),
        model=model,
    )


def _collect_to_anthropic_message(
    events_iter: Any,
    model: str,
) -> _MessageShim:
    """Sync wrapper around ``_collect_to_anthropic_message_async``.

    Handles both sync and async iterables so tests can pass either.
    """
    if hasattr(events_iter, "__aiter__"):
        # Async iterable — run in a new event loop
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _collect_to_anthropic_message_async(events_iter, model)
            )
        finally:
            loop.close()

    # Sync iterable — wrap it as an async iterable
    async def _to_async():
        for item in events_iter:
            yield item

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(
            _collect_to_anthropic_message_async(_to_async(), model)
        )
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Messages namespace shim (duck-types client.messages)
# ---------------------------------------------------------------------------


class _MessagesShim:
    """Duck-types ``anthropic.Anthropic().messages``."""

    def __init__(
        self,
        pool: AuthProfilePool,
        mcp_servers_config: Dict[str, Any],
        model: Optional[str],
        allowed_tools: List[str],
    ):
        self._pool = pool
        self._mcp_servers_config = mcp_servers_config
        self._default_model = model
        self._allowed_tools = allowed_tools

    def create(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        system: Any = None,
        tools: Optional[List[Any]] = None,
        max_tokens: Optional[int] = None,
        extra_env: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> _MessageShim:
        """Invoke the Claude CLI via claude_agent_sdk.query().

        Auth is passed via ``env`` so the real SDK 0.2.87 (which has no
        ``config_dir`` kwarg on ``ClaudeAgentOptions``) works correctly.

        Raises ``ProfilePoolExhausted`` when:
        - No available profile in the pool, OR
        - ``claude_agent_sdk.query()`` raises any exception.
        """
        sdk = _get_claude_agent_sdk()
        if sdk is None:
            raise ImportError(
                "claude_agent_sdk is not installed. "
                "Install with: npm install -g @anthropic-ai/claude-code "
                "or pip install claude-agent-sdk"
            )

        profile = self._pool.select()
        if profile is None:
            raise ProfilePoolExhausted("All profiles are unavailable (cooldown or terminal)")

        effective_model = model or self._default_model or "claude-opus-4-5"
        prompt = _flatten_messages_to_prompt(messages, system)

        # Build the auth env — token + config_dir via environment variables.
        # SECURITY: token is NEVER logged; only profile.label is used in logs.
        auth_env: Dict[str, str] = {
            "CLAUDE_CODE_OAUTH_TOKEN": profile.token,
            "CLAUDE_CONFIG_DIR": profile.config_dir,
        }
        if extra_env:
            auth_env.update(extra_env)

        # Build ClaudeAgentOptions
        options_cls = getattr(sdk, "ClaudeAgentOptions", None)
        if options_cls is None:
            # Older SDK variant: pass auth via env kwarg (no ClaudeAgentOptions class)
            query_kwargs: Dict[str, Any] = {
                "env": auth_env,
                "model": effective_model,
                "allowed_tools": self._allowed_tools,
            }
        else:
            # SDK 0.2.87+: ClaudeAgentOptions has NO config_dir param; auth
            # rides via env={"CLAUDE_CODE_OAUTH_TOKEN": ..., "CLAUDE_CONFIG_DIR": ...}
            query_kwargs = {
                "options": options_cls(
                    model=effective_model,
                    mcp_servers=self._mcp_servers_config,
                    allowed_tools=self._allowed_tools,
                    permission_mode="bypassPermissions",
                    env=auth_env,
                )
            }

        # SECURITY: log only label, never token or config_dir contents
        logger.debug(
            "anthropic_sdk_subprocess_adapter: dispatching via profile=%r model=%r",
            profile.label, effective_model,
        )

        try:
            events = sdk.query(prompt=prompt, **query_kwargs)
            result = _collect_to_anthropic_message(events, model=effective_model)
            self._pool.record_success(profile)
            return result
        except ProfilePoolExhausted:
            raise
        except Exception as exc:
            logger.warning(
                "anthropic_sdk_subprocess_adapter: query failed for profile=%r: %s",
                profile.label, exc,
            )
            raise ProfilePoolExhausted(
                f"Profile {profile.label!r} query failed: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def build_anthropic_sdk_client(
    *,
    profiles: List[Dict[str, Any]],
    mcp_servers: Dict[str, Any],
    model: Optional[str] = None,
    **kw: Any,
) -> Any:
    """Build and return the Anthropic-compatible subprocess shim client.

    Parameters
    ----------
    profiles:
        List of profile dicts, each with keys:

        - ``label``      (str, required) — human name for logs / tracing.
        - ``token``      (str, required) — ``sk-ant-oat01-*`` OAuth setup-token;
          passed to ``ClaudeAgentOptions`` via
          ``env={"CLAUDE_CODE_OAUTH_TOKEN": token}``.
          SECURITY: token is NEVER logged; only ``label`` appears in logs.
        - ``config_dir`` (str, optional) — per-profile isolation directory
          (CLAUDE_CONFIG_DIR).  Defaults to ``/tmp/hermes-claude-<label>``
          when omitted.
        - ``priority``   (int, default 0) — lower wins on count tiebreak.

    mcp_servers:
        Hermes MCP servers config with shape::

            {
              "<server-name>": {
                "type": "http",
                "url": "https://...",
                "headers": {"Authorization": "Bearer <token>"},
              },
              ...
            }

        Converted to ``ClaudeAgentOptions.mcp_servers`` format, which expects
        a dict of ``{name: {"type": "http", "url": ..., "headers": {...}}}``.

    model:
        Default model to pass to the SDK.  Can be overridden per-call.

    Returns a shim object exposing ``.messages.create(...)`` matching the
    ``anthropic.Anthropic`` surface used by Hermes.

    Mode B wiring
    -------------
    ``allowed_tools`` is set to ``["mcp__<server>__*"]`` patterns for each
    configured MCP server, enabling the SDK to run the tool loop internally.

    Auth wiring (SDK 0.2.87+)
    -------------------------
    ``ClaudeAgentOptions`` in SDK 0.2.87 has NO ``config_dir`` parameter.
    Auth is injected via::

        env={
          "CLAUDE_CODE_OAUTH_TOKEN": profile.token,
          "CLAUDE_CONFIG_DIR": profile.config_dir,
        }

    ``permission_mode="bypassPermissions"`` is always set for headless operation.
    """
    pool = AuthProfilePool(profiles)

    # Convert hermes mcp_servers → sdk-compatible mcp_servers dict.
    # Hermes shape: {name: {type, url, headers}} which already matches the
    # ClaudeAgentOptions.mcp_servers expected shape — pass through directly.
    sdk_mcp_servers: Dict[str, Any] = {}
    allowed_tools: List[str] = []
    for server_name, server_cfg in (mcp_servers or {}).items():
        sdk_mcp_servers[server_name] = server_cfg
        # Mode B: include wildcard tool pattern for this MCP server
        allowed_tools.append(f"mcp__{server_name}__*")

    class _ClientShim:
        """Duck-type shim for anthropic.Anthropic."""

        def __init__(self):
            self.messages = _MessagesShim(
                pool=pool,
                mcp_servers_config=sdk_mcp_servers,
                model=model,
                allowed_tools=allowed_tools,
            )

    return _ClientShim()
