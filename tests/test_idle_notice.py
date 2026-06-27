"""Tests for the idle-drop notice quiet-hours window (_in_quiet_hours)."""

from datetime import time as dtime

from jaato_client_telegram.__main__ import _in_quiet_hours


def test_empty_window_never_quiet():
    assert _in_quiet_hours("", dtime(3, 0)) is False


def test_invalid_window_never_quiet():
    assert _in_quiet_hours("garbage", dtime(3, 0)) is False
    assert _in_quiet_hours("23:00", dtime(3, 0)) is False  # missing '-'


def test_overnight_wraparound():
    # "23:00-08:00" spans midnight: quiet from 23:00 through 07:59.
    w = "23:00-08:00"
    assert _in_quiet_hours(w, dtime(0, 30)) is True   # after midnight
    assert _in_quiet_hours(w, dtime(7, 59)) is True   # just before end
    assert _in_quiet_hours(w, dtime(23, 30)) is True  # just after start
    assert _in_quiet_hours(w, dtime(8, 1)) is False   # past the end
    assert _in_quiet_hours(w, dtime(22, 59)) is False  # before the start
    assert _in_quiet_hours(w, dtime(15, 0)) is False  # midday


def test_same_day_window():
    # A non-wrapping window stays within the day.
    w = "13:00-14:00"
    assert _in_quiet_hours(w, dtime(13, 30)) is True
    assert _in_quiet_hours(w, dtime(12, 59)) is False
    assert _in_quiet_hours(w, dtime(14, 0)) is False  # end is exclusive
