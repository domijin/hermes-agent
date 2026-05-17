from tools.send_message_tool import _parse_target_ref


def test_bluebubbles_e164_target_is_explicit_canonical_dm_guid():
    chat_id, thread_id, explicit = _parse_target_ref("bluebubbles", "+15103032825")

    assert chat_id == "any;-;+15103032825"
    assert thread_id is None
    assert explicit is True


def test_bluebubbles_semicolon_guid_target_is_explicit_not_home_fallback():
    chat_id, thread_id, explicit = _parse_target_ref("bluebubbles", "any;-;+15103032825")

    assert chat_id == "any;-;+15103032825"
    assert thread_id is None
    assert explicit is True
