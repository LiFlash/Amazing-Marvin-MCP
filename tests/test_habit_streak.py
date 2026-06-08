"""Pure-function unit tests for habit-streak calculation.

No API key, no network. Tests inject an explicit timezone via the `tz`
parameter so behaviour is deterministic across machines.
"""

import requests
from datetime import datetime, timedelta, date
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from amazing_marvin_mcp.habits import (
    _bucket_by_period,
    _bucket_key,
    _compute_streak,
    _parse_history,
    _prev_bucket,
    _today_bucket,
    get_habit_streak_impl,
)

BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")


def _ts(y, m, d, hh=12, mm=0, tz=BERLIN) -> int:
    """Build an ms-epoch from local wall-clock in the given TZ."""
    return int(datetime(y, m, d, hh, mm, tzinfo=tz).timestamp() * 1000)


# ---------------------------------------------------------------------------
# _parse_history
# ---------------------------------------------------------------------------


def test_parse_history_sorts_and_returns_tuples():
    t1 = _ts(2025, 11, 16)
    t2 = _ts(2025, 11, 17)
    t3 = _ts(2025, 11, 18)
    # Intentionally unsorted: t3 first, then t1, then t2.
    history = [t3, 1, t1, 1, t2, 1]
    result = _parse_history(history)
    assert result == [(t1, 1.0), (t2, 1.0), (t3, 1.0)]
    assert all(isinstance(p, tuple) and len(p) == 2 for p in result)


def test_parse_history_raises_on_odd_length():
    with pytest.raises(ValueError, match="odd length"):
        _parse_history([1700000000000, 1, 1700100000000])


def test_parse_history_empty_returns_empty():
    assert _parse_history([]) == []


def test_parse_history_accepts_float_values():
    t = _ts(2025, 11, 16)
    assert _parse_history([t, 1.5]) == [(t, 1.5)]


# ---------------------------------------------------------------------------
# _bucket_by_period / _bucket_key
# ---------------------------------------------------------------------------


def test_bucket_by_period_day_aggregates_same_day():
    t1 = _ts(2025, 11, 16, 8)
    t2 = _ts(2025, 11, 16, 20)
    pairs = [(t1, 1.0), (t2, 1.0)]
    buckets = _bucket_by_period(pairs, "day", BERLIN)
    assert buckets == {(2025, 11, 16): 2.0}


def test_bucket_by_period_week_uses_iso_week_monday_start():
    # 2025-11-16 = Sunday of ISO week 46
    # 2025-11-17 = Monday of ISO week 47
    sun = _ts(2025, 11, 16, 12)
    mon = _ts(2025, 11, 17, 12)
    pairs = [(sun, 1.0), (mon, 1.0)]
    buckets = _bucket_by_period(pairs, "week", BERLIN)
    assert set(buckets.keys()) == {(2025, 46), (2025, 47)}


def test_bucket_key_unknown_period_raises():
    with pytest.raises(ValueError, match="Unknown period"):
        _bucket_key(_ts(2025, 11, 16), "unknown", BERLIN)


def test_bucket_tz_boundary_23utc_lands_in_next_local_day():
    """A history entry at 2025-11-16 23:00 UTC must bucket to the 17th
    in Europe/Berlin (UTC+1 in November) — i.e. local midnight already
    rolled over."""
    ts_utc_23 = int(
        datetime(2025, 11, 16, 23, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000
    )
    assert _bucket_key(ts_utc_23, "day", BERLIN) == (2025, 11, 17)


def test_bucket_tz_boundary_01utc_lands_in_prev_local_day_new_york():
    """01:00 UTC on the 15th -> 14th local in New York (UTC-5)."""
    ts_utc_01 = int(
        datetime(2025, 4, 15, 1, 0, tzinfo=ZoneInfo("UTC")).timestamp() * 1000
    )
    assert _bucket_key(ts_utc_01, "day", NEW_YORK) == (2025, 4, 14)


# ---------------------------------------------------------------------------
# _prev_bucket sanity (covers ISO-year rollover)
# ---------------------------------------------------------------------------


def test_prev_bucket_week_iso_year_rollover():
    # 2026-W01 -> 2025-W52 (2025 had 52 ISO weeks)
    assert _prev_bucket((2026, 1), "week") == (2025, 52)


def test_prev_bucket_month_january_rolls_to_previous_december():
    assert _prev_bucket((2025, 1), "month") == (2024, 12)


# ---------------------------------------------------------------------------
# _compute_streak
# ---------------------------------------------------------------------------


def test_compute_streak_three_consecutive_days():
    today = (2025, 11, 18)
    buckets = {
        (2025, 11, 16): 1.0,
        (2025, 11, 17): 1.0,
        (2025, 11, 18): 1.0,
    }
    res = _compute_streak(buckets, "day", target=1.0, today_bucket=today)
    assert res == {
        "current": 3,
        "longest": 3,
        "last_fulfilled_bucket": (2025, 11, 18),
    }


def test_compute_streak_today_not_yet_done_does_not_break():
    today = (2025, 11, 19)  # today, NOT in buckets
    buckets = {
        (2025, 11, 16): 1.0,
        (2025, 11, 17): 1.0,
        (2025, 11, 18): 1.0,
    }
    res = _compute_streak(buckets, "day", target=1.0, today_bucket=today)
    assert res["current"] == 3
    assert res["longest"] == 3
    assert res["last_fulfilled_bucket"] == (2025, 11, 18)


def test_compute_streak_gap_breaks_current_but_preserves_longest():
    today = (2025, 11, 20)
    # Pattern: day, day, gap, day  -> longest run = 2, current = 1
    buckets = {
        (2025, 11, 16): 1.0,
        (2025, 11, 17): 1.0,
        # 18th missing
        (2025, 11, 19): 1.0,
        # 20th = today (not yet recorded)
    }
    res = _compute_streak(buckets, "day", target=1.0, today_bucket=today)
    assert res["longest"] == 2
    assert res["current"] == 1  # only the 19th
    assert res["last_fulfilled_bucket"] == (2025, 11, 19)


def test_compute_streak_empty_history():
    res = _compute_streak({}, "day", target=1.0, today_bucket=(2025, 11, 18))
    assert res == {"current": 0, "longest": 0, "last_fulfilled_bucket": None}


def test_compute_streak_week_iso_year_boundary():
    """Streak walks correctly from 2026-W01 backward into 2025-W52."""
    today = (2026, 1)
    buckets = {
        (2025, 51): 1.0,
        (2025, 52): 1.0,
        (2026, 1): 1.0,
    }
    res = _compute_streak(buckets, "week", target=1.0, today_bucket=today)
    assert res["current"] == 3
    assert res["longest"] == 3
    assert res["last_fulfilled_bucket"] == (2026, 1)


# ---------------------------------------------------------------------------
# recordType="number" semantics
# ---------------------------------------------------------------------------


def test_number_record_type_partial_sums_to_target():
    """Two recordings (val=1 and val=2) on the same day with target=3
    must count as fulfilled."""
    t1 = _ts(2025, 11, 18, 8)
    t2 = _ts(2025, 11, 18, 18)
    pairs = _parse_history([t1, 1, t2, 2])
    buckets = _bucket_by_period(pairs, "day", BERLIN)
    today = _bucket_key(t2, "day", BERLIN)
    res = _compute_streak(buckets, "day", target=3.0, today_bucket=today)
    assert buckets[today] == 3.0
    assert res["current"] == 1
    assert res["longest"] == 1


# ---------------------------------------------------------------------------
# IO shell: get_habit_streak_impl
# ---------------------------------------------------------------------------


def _habit_doc(**overrides):
    base = {
        "db": "Habits",
        "_id": "h1",
        "title": "Push-ups",
        "period": "day",
        "target": 1,
        "recordType": "boolean",
        "history": [],
    }
    base.update(overrides)
    return base


def test_get_habit_streak_impl_raises_when_not_a_habit():
    mock = MagicMock()
    mock.get_document.return_value = {"db": "Tasks", "_id": "t1"}
    with pytest.raises(ValueError, match="not a Habit"):
        get_habit_streak_impl(mock, "t1", tz=BERLIN)


def test_get_habit_streak_impl_raises_on_unsupported_period():
    mock = MagicMock()
    mock.get_document.return_value = _habit_doc(period="year")
    with pytest.raises(ValueError, match="unsupported period"):
        get_habit_streak_impl(mock, "h1", tz=BERLIN)


def test_get_habit_streak_impl_handles_missing_history():
    """A freshly-created habit without history must not crash."""
    mock = MagicMock()
    mock.get_document.return_value = _habit_doc(history=[])
    # Force "today" to be predictable by patching datetime.now in module.
    with patch("amazing_marvin_mcp.habits.datetime") as dt_mock:
        # Pass through other attributes
        dt_mock.fromtimestamp = datetime.fromtimestamp
        dt_mock.now.return_value = datetime(2025, 11, 18, 12, 0, tzinfo=BERLIN)
        result = get_habit_streak_impl(mock, "h1", tz=BERLIN)
    assert result["current_streak"] == 0
    assert result["longest_streak"] == 0
    assert result["last_fulfilled_bucket"] is None
    assert result["total_records"] == 0
    assert result["today_fulfilled"] is False


def test_get_habit_streak_impl_target_override_wins():
    t1 = _ts(2025, 11, 18, 8)
    t2 = _ts(2025, 11, 18, 18)
    mock = MagicMock()
    mock.get_document.return_value = _habit_doc(
        recordType="number",
        target=1,
        history=[t1, 1, t2, 2],
    )
    with patch("amazing_marvin_mcp.habits.datetime") as dt_mock:
        dt_mock.fromtimestamp = datetime.fromtimestamp
        dt_mock.now.return_value = datetime(2025, 11, 18, 22, 0, tzinfo=BERLIN)
        # Override target to 5 → today's sum=3 is NOT enough
        result = get_habit_streak_impl(mock, "h1", target_per_period=5.0, tz=BERLIN)
    assert result["target"] == 5.0
    assert result["today_value"] == 3.0
    assert result["today_fulfilled"] is False


def test_get_habit_streak_impl_returns_full_shape():
    t = _ts(2025, 11, 18, 9)
    mock = MagicMock()
    mock.get_document.return_value = _habit_doc(history=[t, 1])
    with patch("amazing_marvin_mcp.habits.datetime") as dt_mock:
        dt_mock.fromtimestamp = datetime.fromtimestamp
        dt_mock.now.return_value = datetime(2025, 11, 18, 20, 0, tzinfo=BERLIN)
        result = get_habit_streak_impl(mock, "h1", tz=BERLIN)
    for key in (
        "habit_id",
        "title",
        "period",
        "target",
        "record_type",
        "current_streak",
        "longest_streak",
        "last_fulfilled_bucket",
        "today_fulfilled",
        "today_value",
        "total_records",
    ):
        assert key in result, f"missing key {key}"
    assert result["habit_id"] == "h1"
    assert result["period"] == "day"
    assert result["current_streak"] == 1
    assert result["today_fulfilled"] is True


# ---------------------------------------------------------------------------
# _today_bucket smoke (just ensures it uses the tz it gets handed)
# ---------------------------------------------------------------------------


def test_today_bucket_smoke():
    bucket = _today_bucket("day", BERLIN)
    now = datetime.now(tz=BERLIN)
    assert bucket == (now.year, now.month, now.day)


# ---------------------------------------------------------------------------
# Additional _bucket_by_period / _bucket_key tests
# ---------------------------------------------------------------------------


def test_bucket_period_month_groups_per_calendar_month():
    """Three entries in the same calendar month produce one bucket
    whose value equals the sum of all three values."""
    t1 = _ts(2025, 5, 1)
    t2 = _ts(2025, 5, 15)
    t3 = _ts(2025, 5, 28)
    pairs = [(t1, 1.0), (t2, 2.0), (t3, 3.0)]
    buckets = _bucket_by_period(pairs, "month", BERLIN)
    assert buckets == {(2025, 5): 6.0}


def test_bucket_by_period_returns_empty_dict_when_pairs_empty():
    """Empty pairs list must return an empty dict, not crash."""
    result = _bucket_by_period([], "day", BERLIN)
    assert result == {}


def test_bucket_key_day_uses_year_month_day_tuple():
    """_bucket_key for period='day' must return exactly (year, month, day)
    as a 3-tuple of ints."""
    ts = _ts(2025, 4, 7)
    key = _bucket_key(ts, "day", BERLIN)
    assert key == (2025, 4, 7)
    assert isinstance(key, tuple)
    assert len(key) == 3
    assert all(isinstance(x, int) for x in key)


# ---------------------------------------------------------------------------
# Additional _compute_streak tests
# ---------------------------------------------------------------------------


def test_streak_single_bucket_today_meets_target():
    """A single bucket for today that meets target -> current=1, longest=1."""
    today = (2025, 11, 18)
    buckets = {(2025, 11, 18): 1.0}
    res = _compute_streak(buckets, "day", target=1.0, today_bucket=today)
    assert res["current"] == 1
    assert res["longest"] == 1
    assert res["last_fulfilled_bucket"] == (2025, 11, 18)


def test_streak_today_fails_target_with_prev_filled():
    """today value < target: cursor steps back to yesterday.
    Yesterday meets target -> current=1, today_fulfilled is tracked
    outside _compute_streak (via today_value in get_habit_streak_impl),
    but the streak result shows current=1 from yesterday."""
    today = (2025, 11, 18)
    buckets = {
        # yesterday: value=3 meets target=3
        (2025, 11, 17): 3.0,
        # today: value=2 does NOT meet target=3
        (2025, 11, 18): 2.0,
    }
    res = _compute_streak(buckets, "day", target=3.0, today_bucket=today)
    # today fails -> cursor steps back to 17th, which is fulfilled -> current=1
    assert res["current"] == 1
    assert res["last_fulfilled_bucket"] == (2025, 11, 17)


def test_streak_boolean_recordtype_sum_equals_count():
    """Three boolean entries (val=1 each) on the same day with target=2.
    Sum=3 >= target=2 -> bucket is fulfilled -> current=1."""
    t1 = _ts(2025, 11, 18, 8)
    t2 = _ts(2025, 11, 18, 12)
    t3 = _ts(2025, 11, 18, 18)
    pairs = _parse_history([t1, 1, t2, 1, t3, 1])
    buckets = _bucket_by_period(pairs, "day", BERLIN)
    today = _bucket_key(t1, "day", BERLIN)
    res = _compute_streak(buckets, "day", target=2.0, today_bucket=today)
    assert buckets[today] == 3.0
    assert res["current"] == 1
    assert res["longest"] == 1


def test_streak_number_recordtype_overfulfillment():
    """recordType=number, target=3, one entry with val=5 today.
    Sum=5 >= 3 -> fulfilled -> current=1."""
    t = _ts(2025, 11, 18, 10)
    pairs = _parse_history([t, 5])
    buckets = _bucket_by_period(pairs, "day", BERLIN)
    today = _bucket_key(t, "day", BERLIN)
    res = _compute_streak(buckets, "day", target=3.0, today_bucket=today)
    assert buckets[today] == 5.0
    assert res["current"] == 1
    assert res["longest"] == 1


def test_streak_period_week_with_partial_current_week():
    """period=week, target=3.
    Current week has only 2 records (sum=2 < target) -> not fulfilled.
    Cursor steps back to previous week. Previous two weeks are fulfilled.
    -> current=2 (from the two prior fulfilled weeks)."""
    # Build a Monday of the current ISO week from a known date
    # Use 2025-11-17 (Monday of week 47) as "current week monday"
    current_week_monday = date(2025, 11, 17)
    current_iso = current_week_monday.isocalendar()
    current_week_key = (current_iso.year, current_iso.week)

    prev_week_monday = current_week_monday - timedelta(days=7)
    prev_iso = prev_week_monday.isocalendar()
    prev_week_key = (prev_iso.year, prev_iso.week)

    prev2_week_monday = current_week_monday - timedelta(days=14)
    prev2_iso = prev2_week_monday.isocalendar()
    prev2_week_key = (prev2_iso.year, prev2_iso.week)

    buckets = {
        prev2_week_key: 3.0,   # fulfilled
        prev_week_key: 3.0,    # fulfilled
        current_week_key: 2.0,  # NOT fulfilled (only 2 entries so far)
    }
    res = _compute_streak(buckets, "week", target=3.0, today_bucket=current_week_key)
    # current week not fulfilled -> step back to prev_week (fulfilled),
    # then prev2_week (fulfilled), then prev3 (missing) -> stop
    assert res["current"] == 2
    assert res["longest"] == 2


def test_streak_target_default_is_one_when_doc_target_is_zero():
    """Habit doc with target=0 -> treated as 1 in get_habit_streak_impl.
    One record today -> current=1."""
    t = _ts(2025, 11, 18, 9)
    mock = MagicMock()
    mock.get_document.return_value = _habit_doc(target=0, history=[t, 1])
    with patch("amazing_marvin_mcp.habits.datetime") as dt_mock:
        dt_mock.fromtimestamp = datetime.fromtimestamp
        dt_mock.now.return_value = datetime(2025, 11, 18, 20, 0, tzinfo=BERLIN)
        result = get_habit_streak_impl(mock, "h1", tz=BERLIN)
    assert result["target"] == 1.0
    assert result["current_streak"] == 1
    assert result["today_fulfilled"] is True


def test_streak_target_default_is_one_when_doc_target_missing():
    """Habit doc without 'target' field -> treated as 1 in get_habit_streak_impl.
    One record today -> current=1."""
    t = _ts(2025, 11, 18, 9)
    mock = MagicMock()
    doc = _habit_doc(history=[t, 1])
    del doc["target"]
    mock.get_document.return_value = doc
    with patch("amazing_marvin_mcp.habits.datetime") as dt_mock:
        dt_mock.fromtimestamp = datetime.fromtimestamp
        dt_mock.now.return_value = datetime(2025, 11, 18, 20, 0, tzinfo=BERLIN)
        result = get_habit_streak_impl(mock, "h1", tz=BERLIN)
    assert result["target"] == 1.0
    assert result["current_streak"] == 1
    assert result["today_fulfilled"] is True


def test_streak_target_override_wins_over_doc_default():
    """doc.target=1, call with target_per_period=5.
    Records today: three entries val=1 each -> sum=3 < 5 -> today_fulfilled=False."""
    t1 = _ts(2025, 11, 18, 8)
    t2 = _ts(2025, 11, 18, 12)
    t3 = _ts(2025, 11, 18, 18)
    mock = MagicMock()
    mock.get_document.return_value = _habit_doc(
        recordType="number",
        target=1,
        history=[t1, 1, t2, 1, t3, 1],
    )
    with patch("amazing_marvin_mcp.habits.datetime") as dt_mock:
        dt_mock.fromtimestamp = datetime.fromtimestamp
        dt_mock.now.return_value = datetime(2025, 11, 18, 22, 0, tzinfo=BERLIN)
        result = get_habit_streak_impl(mock, "h1", target_per_period=5.0, tz=BERLIN)
    assert result["target"] == 5.0
    assert result["today_value"] == 3.0
    assert result["today_fulfilled"] is False


# ---------------------------------------------------------------------------
# Additional IO-shell tests
# ---------------------------------------------------------------------------


def test_get_habit_streak_propagates_404():
    """When api_client.get_document raises requests.HTTPError,
    get_habit_streak_impl must let it propagate (no swallowing)."""
    mock = MagicMock()
    http_error = requests.HTTPError(response=MagicMock(status_code=404))
    mock.get_document.side_effect = http_error
    with pytest.raises(requests.HTTPError):
        get_habit_streak_impl(mock, "h1", tz=BERLIN)


def test_get_habit_streak_calls_get_document_with_habit_id():
    """get_habit_streak_impl must call api_client.get_document(habit_id),
    not any other method such as get_habit."""
    mock = MagicMock()
    mock.get_document.return_value = _habit_doc()
    with patch("amazing_marvin_mcp.habits.datetime") as dt_mock:
        dt_mock.fromtimestamp = datetime.fromtimestamp
        dt_mock.now.return_value = datetime(2025, 11, 18, 12, 0, tzinfo=BERLIN)
        get_habit_streak_impl(mock, "my-habit-id", tz=BERLIN)
    mock.get_document.assert_called_once_with("my-habit-id")
    mock.get_habit.assert_not_called()
