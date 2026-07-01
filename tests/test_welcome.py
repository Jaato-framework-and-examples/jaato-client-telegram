"""First-contact welcome: one-time, atomic, persisted per chat."""

from jaato_client_telegram.welcome_store import (
    WELCOME_PREFIX,
    WELCOME_START,
    WelcomeStore,
)


def test_claim_is_true_once_then_false():
    store = WelcomeStore("")  # in-memory
    assert store.claim_first_contact(42) is True     # first contact
    assert store.claim_first_contact(42) is False    # already welcomed
    assert store.claim_first_contact(99) is True      # a different chat
    assert store.claim_first_contact(99) is False


def test_persists_across_reload(tmp_path):
    path = str(tmp_path / "welcomed_chats.json")
    s1 = WelcomeStore(path)
    assert s1.claim_first_contact(7) is True
    # A fresh store loading the same file must not re-welcome.
    s2 = WelcomeStore(path)
    assert s2.claim_first_contact(7) is False
    assert s2.claim_first_contact(8) is True


def test_unconfigured_is_in_memory_only(tmp_path):
    # Empty path = no file written, gating still works within the process.
    store = WelcomeStore("")
    assert store.claim_first_contact(1) is True
    assert store.claim_first_contact(1) is False
    assert not list(tmp_path.iterdir())              # nothing persisted


def test_directives_are_distinct_and_nonempty():
    assert WELCOME_START and WELCOME_PREFIX
    assert WELCOME_PREFIX.endswith("\n\n")           # rides in front of the message
    assert "first contact" in WELCOME_START.lower()


if __name__ == "__main__":
    import sys

    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
