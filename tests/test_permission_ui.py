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
    h = PermissionHandler()  # default code_extensions = notebook_execute:py
    big = "x = 1\n" * 2000                  # > _PARAM_EXPAND_MAX, no file written → file
    text, _, files = h.create_permission_ui(_event({"code": big}, _ALL_OPTIONS), 1)
    assert len(files) == 1
    fname, content = files[0]
    assert fname == "code.py"               # no real filename in code → {param}.{ext}, no tool prefix
    assert content == big                   # the WHOLE thing travels as a file
    assert "full value sent as" in text     # prompt tells the user where it went
    assert len(text) < 4096                 # prompt itself stays within Telegram's limit


def test_overflow_uses_real_filename_from_draft_write():
    # Tool-creating cell writes tool_drafts/<name>.py → name the attachment after it.
    # The code STRING itself must be big enough to overflow (a long literal body).
    h = PermissionHandler()
    code = (
        'with open("tool_drafts/moon_phase.py", "w") as f:\n    f.write("""\n'
        + ("# moon phase logic line\n" * 300)  # ~7k chars of code text
        + '""")\n'
    )
    _, _, files = h.create_permission_ui(_event({"code": code}, _ALL_OPTIONS), 1)
    assert files[0][0] == "moon_phase.py"


def test_overflow_uses_open_write_target_when_no_draft_path():
    h = PermissionHandler()
    code = 'open("report.csv", "w").write("""\n' + ("row,value\n" * 500) + '""")\n'
    _, _, files = h.create_permission_ui(_event({"code": code}, _ALL_OPTIONS), 1)
    assert files[0][0] == "report.csv"


def test_overflow_fallback_for_unknown_tool_drops_tool_name():
    h = PermissionHandler(code_extensions_str="notebook_execute:py")
    big = "data\n" * 2000                    # no file write → {param}.{ext}
    ev = _event({"blob": big}, _ALL_OPTIONS)
    ev.tool_name = "some_other_tool"
    _, _, files = h.create_permission_ui(ev, 1)
    assert files[0][0] == "blob.txt"


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


# --- register_tool install: show the code being installed (stage 2) -----------

def _reg_event(name, action=None):
    args = {"name": name}
    if action:
        args["action"] = action
    return SimpleNamespace(
        tool_name="register_tool", tool_args=args,
        response_options=_ALL_OPTIONS, request_id="req1",
    )


def test_register_tool_install_attaches_draft_code(tmp_path):
    (tmp_path / "tool_drafts").mkdir()
    (tmp_path / "tool_drafts" / "greeter.py").write_text(
        "TOOL_SCHEMA = {}\nasync def execute(a, c):\n    return {}  # SECRET_MARKER\n"
    )
    h = PermissionHandler(workspace=str(tmp_path))
    text, _, files = h.create_permission_ui(_reg_event("greeter"), 1)
    assert any(fn == "greeter.py" and "SECRET_MARKER" in body for fn, body in files)
    assert "UNCONFINED" in text and "greeter.py" in text


def test_register_tool_edit_does_not_review_code(tmp_path):
    (tmp_path / "tool_drafts").mkdir()
    (tmp_path / "tool_drafts" / "greeter.py").write_text("x")
    h = PermissionHandler(workspace=str(tmp_path))
    text, _, files = h.create_permission_ui(_reg_event("greeter", action="edit"), 1)
    assert not any(fn == "greeter.py" for fn, _ in files)
    assert "UNCONFINED" not in text


def test_register_tool_missing_draft_warns(tmp_path):
    h = PermissionHandler(workspace=str(tmp_path))  # no draft written
    text, _, files = h.create_permission_ui(_reg_event("ghost"), 1)
    assert not files
    assert "couldn't find its code" in text


def test_register_tool_unsafe_name_ignored(tmp_path):
    h = PermissionHandler(workspace=str(tmp_path))
    text, _, files = h.create_permission_ui(_reg_event("../../etc/passwd"), 1)
    assert not files and "UNCONFINED" not in text


def test_non_register_tool_has_no_install_warning(tmp_path):
    h = PermissionHandler(workspace=str(tmp_path))
    text, _, _ = h.create_permission_ui(_event({"path": "a.txt"}, _ALL_OPTIONS), 1)
    assert "UNCONFINED" not in text
