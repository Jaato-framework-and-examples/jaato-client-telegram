"""VoiceModeStore — per-chat voice-reply mode (on / off / auto=reply-in-kind)."""

import json

from jaato_client_telegram.voice_mode_store import VOICE_HINT, VoiceModeStore


def test_auto_is_reply_in_kind():
    s = VoiceModeStore()
    # unset ⇒ speak iff the inbound was a voice note
    assert s.wants_voice(1, inbound_is_audio=True) is True
    assert s.wants_voice(1, inbound_is_audio=False) is False


def test_on_speaks_everything_off_speaks_nothing():
    s = VoiceModeStore()
    s.set(1, True)
    assert s.wants_voice(1, inbound_is_audio=False) is True   # even text
    s.set(1, False)
    assert s.wants_voice(1, inbound_is_audio=True) is False    # even a voice note


def test_clear_returns_to_auto():
    s = VoiceModeStore()
    s.set(1, False)
    s.clear(1)
    assert s.get(1) is None
    assert s.wants_voice(1, inbound_is_audio=True) is True


def test_persists_across_instances(tmp_path):
    p = str(tmp_path / "voice_mode.json")
    s = VoiceModeStore(p)
    s.set(7, True)
    s.set(9, False)
    # a fresh instance reads the same file
    assert VoiceModeStore(p).get(7) is True
    assert VoiceModeStore(p).get(9) is False
    # stored keyed by string chat id
    assert json.loads(open(p).read()) == {"7": True, "9": False}


def test_voice_hint_names_the_voz_tier():
    assert "voz" in VOICE_HINT
