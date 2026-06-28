"""Tests for the permission-request UI: reviewable params + decluttered buttons."""

from types import SimpleNamespace

from jaato_client_telegram.permissions import PermissionHandler


def _event(tool_args, options):
    return SimpleNamespace(
        tool_name="notebook_execute",
        tool_args=tool_args,
        response_options=options,
        request_id="req1",
    )


_ALL_OPTIONS = [
    {"key": "y", "label": "yes"},
    {"key": "n", "label": "no"},
    {"key": "a", "label": "always"},
    {"key": "nv", "label": "never"},
    {"key": "t", "label": "turn"},
    {"key": "i", "label": "idle"},
    {"key": "o", "label": "once"},
    {"key": "al", "label": "all"},
    {"key": "c", "label": "comment"},
]


def _button_labels(keyboard):
    return [b.text for row in keyboard.inline_keyboard for b in row]


# ── param rendering: review the full content ─────────────────────────────────

def test_short_param_stays_inline():
    h = PermissionHandler()
    text, _, files = h.create_permission_ui(_event({"path": "a.txt"}, _ALL_OPTIONS), 1)
    assert "<code>path</code>: a.txt" in text
    assert files == []


def test_long_multiline_param_is_full_and_expandable():
    h = PermissionHandler()
    code = "import os\n" + "\n".join(f"line_{i} = {i}" for i in range(40))  # multi-line, < expand max
    text, _, files = h.create_permission_ui(_event({"code": code}, _ALL_OPTIONS), 1)
    assert "<blockquote expandable>" in text
    assert "line_39 = 39" in text          # FULL content present (not truncated at 40 chars)
    assert files == []                     # fits in-message, no file


def test_huge_param_overflows_to_file_with_preview():
    h = PermissionHandler()
    big = "x = 1\n" * 2000                  # > _PARAM_EXPAND_MAX → file
    text, _, files = h.create_permission_ui(_event({"code": big}, _ALL_OPTIONS), 1)
    assert len(files) == 1
    fname, content = files[0]
    assert fname == "notebook_execute.code.txt"
    assert content == big                  # the WHOLE thing travels as a file
    assert "full value sent as" in text    # prompt tells the user where it went
    assert len(text) < 4096                # prompt itself stays within Telegram's limit


# ── button declutter ─────────────────────────────────────────────────────────

def test_only_primary_actions_shown_by_default():
    h = PermissionHandler()  # default primary_actions = yes,no,always,never
    _, keyboard, _ = h.create_permission_ui(_event({"x": "1"}, _ALL_OPTIONS), 1)
    labels = " ".join(_button_labels(keyboard)).lower()
    for keep in ("yes", "no", "always", "never"):
        assert keep in labels
    for drop in ("turn", "idle", "once", "all", "comment"):
        assert drop not in labels


def test_configurable_primary_actions():
    h = PermissionHandler(primary_actions_str="yes,no,once")
    _, keyboard, _ = h.create_permission_ui(_event({"x": "1"}, _ALL_OPTIONS), 1)
    labels = " ".join(_button_labels(keyboard)).lower()
    assert "once" in labels and "always" not in labels


def test_empty_primary_actions_is_legacy_show_all():
    # Empty => fall back to the action denylist (which leaves all these, since they
    # carry no 'action'); i.e. no label-based declutter.
    h = PermissionHandler(primary_actions_str="")
    _, keyboard, _ = h.create_permission_ui(_event({"x": "1"}, _ALL_OPTIONS), 1)
    assert len(_button_labels(keyboard)) == len(_ALL_OPTIONS)
