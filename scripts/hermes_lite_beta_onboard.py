#!/usr/bin/env python3
"""Deterministic Hermes-lite BlueBubbles beta onboarding.

This intentionally avoids LLM chat loops. It performs three guarded steps:
1. Add the E.164 phone number to BLUEBUBBLES_ALLOWED_USERS.
2. Seed Hermes-lite session/channel-directory state for exact target routing.
3. Optionally send one greeting, but only after querying the exact chat and
   proving the exact greeting has not already been sent.

Default mode is dry-run. Use --send for the one side-effectful greeting send.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_LITE_AGENT_HOME = Path("/Users/hermes-life/hermes/lite/agent")
DEFAULT_ENV_PATH = DEFAULT_LITE_AGENT_HOME / "config" / ".env"
DEFAULT_STATE_DIR = DEFAULT_LITE_AGENT_HOME / "state"
GREETINGS_BY_LANGUAGE = {
    "en": (
        "Hi — you’re approved for the private Life Hack beta. This is an experimental iMessage assistant: "
        "a low-bandwidth router to an internet-connected agent for moments when browsing is expensive, blocked, or unavailable — like flights, travel, or spotty networks.\n\n"
        "Try short prompts such as: ‘What’s the weather when I land?’ or ‘Summarize today’s AI news in 3 bullets.’\n\n"
        "It’s experimental and not for emergencies, medical, legal, or financial advice. What’s your preferred communication language? — Mini"
    ),
    "zh": (
        "你好 — 你已经通过 Life Hack 私人测试版。它是一个实验性的 iMessage 助手：把低带宽短信变成通往联网 agent 的入口，"
        "适合在飞机上、旅行中、网络受限或浏览器不可用时，用很短的消息获取压缩后的答案。\n\n"
        "你可以先试：『我落地时天气怎样？』或『用 3 条总结今天的 AI 新闻。』\n\n"
        "它还在测试中，不能替代紧急服务、医疗、法律或金融建议。你偏好的沟通语言是什么？— Mini"
    ),
}
DEFAULT_GREETING = GREETINGS_BY_LANGUAGE["en"]


def choose_language(phone: str) -> str:
    if phone.startswith("+86"):
        return "zh"
    return "en"


def choose_greeting(phone: str) -> str:
    return GREETINGS_BY_LANGUAGE[choose_language(phone)]


@dataclass(frozen=True)
class GreetingDecision:
    send: bool
    reason: str
    existing_count: int


@dataclass(frozen=True)
class SendOutcome:
    sent: bool
    message_id: str | None
    chat_id: str
    before_count: int
    after_count: int
    reason: str


def normalize_us_phone(raw: str) -> str:
    text = (raw or "").strip()
    digits = re.sub(r"\D", "", text)
    if text.startswith("+"):
        if not re.fullmatch(r"\+\d{7,15}", text):
            raise ValueError(f"not an E.164 phone number: {raw!r}")
        return text
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    raise ValueError(f"expected US 10-digit or E.164 phone number, got: {raw!r}")


def canonical_chat_id(phone: str) -> str:
    return f"any;-;{phone}"


def session_key(phone: str) -> str:
    return f"agent:main:bluebubbles:dm:{canonical_chat_id(phone)}"


def load_env_values(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def add_allowed_user(env_path: Path, phone: str) -> bool:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True) if env_path.exists() else []
    key = "BLUEBUBBLES_ALLOWED_USERS"
    found = False
    changed = False
    new_lines: list[str] = []
    for line in lines:
        if line.startswith(f"{key}="):
            found = True
            newline = "\n" if line.endswith("\n") else ""
            value = line.rstrip("\n").split("=", 1)[1]
            users = [u.strip() for u in value.split(",") if u.strip()]
            if phone not in users:
                users.append(phone)
                changed = True
            new_lines.append(f"{key}={','.join(users)}{newline}")
        else:
            new_lines.append(line)
    if not found:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] += "\n"
        new_lines.append(f"{key}={phone}\n")
        changed = True
    if changed:
        env_path.write_text("".join(new_lines), encoding="utf-8")
    return changed


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def seed_channel_directory(path: Path, phone: str) -> bool:
    data = _read_json(path, {"platforms": {"bluebubbles": []}})
    if not isinstance(data, dict):
        data = {"platforms": {"bluebubbles": []}}
    platforms = data.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        platforms = {}
        data["platforms"] = platforms
    entries = platforms.setdefault("bluebubbles", [])
    if not isinstance(entries, list):
        entries = []
        platforms["bluebubbles"] = entries
    # Clean up an older local script bug that wrote a top-level bluebubbles
    # list; the gateway reads platforms.bluebubbles.
    data.pop("bluebubbles", None)
    chat_id = canonical_chat_id(phone)
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == chat_id:
            _write_json(path, data)
            return False
    entries.append({"id": chat_id, "name": phone, "type": "dm", "thread_id": None})
    _write_json(path, data)
    return True


def seed_session(path: Path, phone: str) -> bool:
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    key = session_key(phone)
    if key in data:
        return False
    chat_id = canonical_chat_id(phone)
    now = int(time.time())
    data[key] = {
        "session_key": key,
        "platform": "bluebubbles",
        "chat_type": "dm",
        "chat_id": chat_id,
        "display_name": phone,
        "created_at": now,
        "updated_at": now,
        "metadata": {
            "platform": "bluebubbles",
            "chat_id": chat_id,
            "chat_name": phone,
            "chat_type": "dm",
            "user_id": phone,
            "user_name": phone,
        },
    }
    _write_json(path, data)
    return True


def count_exact_greetings(messages: Iterable[dict[str, Any]], greeting: str) -> int:
    return sum(1 for message in messages if message.get("isFromMe") is True and message.get("text") == greeting)


def should_send_greeting(messages: Iterable[dict[str, Any]], greeting: str) -> GreetingDecision:
    existing = count_exact_greetings(messages, greeting)
    if existing > 0:
        return GreetingDecision(False, "already_sent", existing)
    return GreetingDecision(True, "ok", 0)


class BlueBubblesClient:
    def __init__(self, server_url: str, password: str):
        if not server_url or not password:
            raise ValueError("BlueBubbles server URL/password are required")
        self.server_url = server_url.rstrip("/")
        self.password = password

    def _url(self, path: str) -> str:
        sep = "&" if "?" in path else "?"
        return f"{self.server_url}{path}{sep}password={urllib.parse.quote(self.password, safe='')}"

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url(path),
            data=data,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))

    def messages(self, chat_id: str, limit: int = 50) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(chat_id, safe="")
        payload = self._request("GET", f"/api/v1/chat/{encoded}/message?limit={limit}&offset=0")
        data = payload.get("data") or []
        return data if isinstance(data, list) else []

    def chat_exists(self, chat_id: str) -> bool:
        try:
            self.messages(chat_id, limit=1)
            return True
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            raise

    def send_text(self, chat_id: str, phone: str, text: str) -> str:
        if self.chat_exists(chat_id):
            result = self._request(
                "POST",
                "/api/v1/message/text",
                {"chatGuid": chat_id, "tempGuid": f"temp-{time.time()}", "message": text},
            )
        else:
            result = self._request(
                "POST",
                "/api/v1/chat/new",
                {"addresses": [phone], "message": text, "tempGuid": f"temp-{time.time()}"},
            )
        data = result.get("data") or {}
        return str(data.get("guid") or data.get("messageGuid") or "ok")


def send_one_guarded_greeting(client: BlueBubblesClient, phone: str, greeting: str) -> SendOutcome:
    chat_id = canonical_chat_id(phone)
    before_messages = client.messages(chat_id, limit=50) if client.chat_exists(chat_id) else []
    decision = should_send_greeting(before_messages, greeting)
    if not decision.send:
        return SendOutcome(False, None, chat_id, decision.existing_count, decision.existing_count, decision.reason)

    message_id = client.send_text(chat_id, phone, greeting)
    after_messages = client.messages(chat_id, limit=50)
    after_count = count_exact_greetings(after_messages, greeting)
    if after_count != decision.existing_count + 1:
        return SendOutcome(False, message_id, chat_id, decision.existing_count, after_count, "post_send_count_mismatch")
    return SendOutcome(True, message_id, chat_id, decision.existing_count, after_count, "sent")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phone", help="US 10-digit or E.164 phone number")
    parser.add_argument("--env", type=Path, default=DEFAULT_ENV_PATH)
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--greeting", default=None, help="Override the country-code-selected FTUE greeting.")
    parser.add_argument("--send", action="store_true", help="Actually send one guarded greeting. Default is dry-run.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    phone = normalize_us_phone(args.phone)
    greeting = args.greeting or choose_greeting(phone)
    env_changed = add_allowed_user(args.env, phone)
    state = args.state
    directory_changed = seed_channel_directory(state / "channel_directory.json", phone)
    session_changed = seed_session(state / "sessions" / "sessions.json", phone)

    result: dict[str, Any] = {
        "phone": phone,
        "chat_id": canonical_chat_id(phone),
        "allowlist_changed": env_changed,
        "channel_directory_changed": directory_changed,
        "session_changed": session_changed,
        "sent": False,
        "dry_run": not args.send,
    }

    env_values = load_env_values(args.env)
    if args.send:
        client = BlueBubblesClient(
            env_values.get("BLUEBUBBLES_SERVER_URL", ""),
            env_values.get("BLUEBUBBLES_PASSWORD", ""),
        )
        outcome = send_one_guarded_greeting(client, phone, greeting)
        result.update(
            {
                "sent": outcome.sent,
                "message_id": outcome.message_id,
                "chat_id": outcome.chat_id,
                "before_count": outcome.before_count,
                "after_count": outcome.after_count,
                "reason": outcome.reason,
            }
        )
        if not outcome.sent and outcome.reason == "post_send_count_mismatch":
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
            return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
