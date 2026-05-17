import json
from pathlib import Path

from scripts.hermes_lite_beta_onboard import (
    DEFAULT_GREETING,
    GREETINGS_BY_LANGUAGE,
    add_allowed_user,
    choose_greeting,
    choose_language,
    count_exact_greetings,
    normalize_us_phone,
    seed_channel_directory,
    seed_session,
    should_send_greeting,
)


def test_normalize_us_phone_accepts_ten_digit_number():
    assert normalize_us_phone("5103032825") == "+15103032825"


def test_choose_language_uses_country_code_and_defaults_to_english():
    assert choose_language("+8615888819928") == "zh"
    assert choose_language("+15103032825") == "en"
    assert choose_language("+447700900123") == "en"


def test_choose_greeting_matches_landing_page_positioning_and_asks_language():
    chinese = choose_greeting("+8615888819928")
    english = choose_greeting("+15103032825")

    assert chinese == GREETINGS_BY_LANGUAGE["zh"]
    assert "iMessage" in english
    assert "preferred communication language" in english
    assert "偏好的沟通语言" in chinese


def test_add_allowed_user_is_idempotent_and_preserves_other_env_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("FOO=bar\nBLUEBUBBLES_ALLOWED_USERS=+15550000000,+15103032825\n", encoding="utf-8")

    changed = add_allowed_user(env, "+15103032825")

    assert changed is False
    assert env.read_text(encoding="utf-8") == "FOO=bar\nBLUEBUBBLES_ALLOWED_USERS=+15550000000,+15103032825\n"


def test_seed_state_creates_exact_dm_target_without_duplicates(tmp_path):
    channel_dir = tmp_path / "channel_directory.json"
    sessions = tmp_path / "sessions.json"
    channel_dir.write_text(json.dumps({"platforms": {"bluebubbles": []}}), encoding="utf-8")
    sessions.write_text(json.dumps({}), encoding="utf-8")

    seed_channel_directory(channel_dir, "+15103032825")
    seed_channel_directory(channel_dir, "+15103032825")
    seed_session(sessions, "+15103032825")
    seed_session(sessions, "+15103032825")

    directory = json.loads(channel_dir.read_text(encoding="utf-8"))
    assert "bluebubbles" not in directory
    assert directory["platforms"]["bluebubbles"] == [
        {"id": "any;-;+15103032825", "name": "+15103032825", "type": "dm", "thread_id": None}
    ]
    data = json.loads(sessions.read_text(encoding="utf-8"))
    assert list(data) == ["agent:main:bluebubbles:dm:any;-;+15103032825"]
    assert data["agent:main:bluebubbles:dm:any;-;+15103032825"]["metadata"]["chat_id"] == "any;-;+15103032825"


def test_should_send_greeting_blocks_existing_exact_greeting():
    messages = [
        {"text": DEFAULT_GREETING, "isFromMe": True},
        {"text": "Mini", "isFromMe": False},
    ]

    decision = should_send_greeting(messages, DEFAULT_GREETING)

    assert decision.send is False
    assert decision.reason == "already_sent"
    assert decision.existing_count == 1


def test_should_send_greeting_allows_zero_existing_greetings():
    decision = should_send_greeting([], DEFAULT_GREETING)

    assert decision.send is True
    assert decision.reason == "ok"
    assert decision.existing_count == 0


def test_count_exact_greetings_only_counts_outbound_exact_text():
    messages = [
        {"text": DEFAULT_GREETING, "isFromMe": True},
        {"text": DEFAULT_GREETING, "isFromMe": False},
        {"text": DEFAULT_GREETING + " extra", "isFromMe": True},
    ]

    assert count_exact_greetings(messages, DEFAULT_GREETING) == 1
