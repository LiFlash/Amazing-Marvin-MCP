"""Pure-core/IO-shell implementation for habit streak calculations.

The pure helpers (`_parse_history`, `_bucket_by_period`, `_compute_streak`,
`_bucket_key`, `_today_bucket`, `_prev_bucket`) operate on plain Python
values and accept an explicit timezone for testability.  The IO shell
`get_habit_streak_impl` fetches the habit document via the API client and
delegates to those helpers.

The `history` field of a Habit-Doc is a flat array
``[t1, v1, t2, v2, ...]`` of ms-epoch timestamps and recorded values, not
necessarily in chronological order.  See `test/plans/couchdb-smartlists.md`
section C for the full behaviour contract.
"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ALLOWED_PERIODS = {"day", "week", "month"}

HABIT_DB = "Habits"


def get_enriched_habits(api_client) -> list[dict]:
    """Return all habits as FULL CouchDB documents (with title, target, db,
    `period` as a string, recordType, history, ...).

    Marvin's REST endpoint ``/api/habits`` returns a reduced projection
    without ``db``/``title``/``target`` and with ``period`` encoded as
    an int. This helper unifies both code paths:

    - When CouchDB direct access is configured (``has_couchdb=True``),
      one Mango ``_find`` query returns the full docs.
    - Otherwise, the REST endpoint is used as the habit-id source and
      each habit is enriched via a per-id ``get_document`` call
      (``/api/doc?id=…``). Costs N+1 round trips for N habits.
    """
    if getattr(api_client, "has_couchdb", False):
        return api_client.find_docs({"db": HABIT_DB}, limit=500)

    raw = api_client.get_habits()
    enriched: list[dict] = []
    for h in raw:
        hid = h.get("habitId") or h.get("_id")
        if not hid:
            continue
        try:
            doc = api_client.get_document(hid)
        except Exception:  # noqa: BLE001
            # On lookup failure, fall back to the REST projection so the
            # caller at least gets habitId + history. Title etc. remain
            # unset.
            enriched.append({**h, "_id": hid})
            continue
        enriched.append({**h, **doc, "_id": hid})
    return enriched


def get_enriched_habit(api_client, habit_id: str) -> dict:
    """Return one habit as the FULL CouchDB document. Always uses
    ``get_document`` (``/api/doc?id=…``) for the canonical shape."""
    return api_client.get_document(habit_id)


def _parse_history(history: list) -> list[tuple[int, float]]:
    """Convert flat ``[t1, v1, t2, v2, ...]`` into a sorted list of
    ``(ts_ms, value)`` tuples.

    Raises:
        ValueError: when the flat history has an odd length (data corruption).
    """
    if len(history) % 2 != 0:
        raise ValueError(f"Habit history has odd length {len(history)}")
    pairs = list(zip(history[::2], history[1::2], strict=False))
    # Defensive cast — recordType=number can produce floats.
    typed_pairs: list[tuple[int, float]] = [(int(t), float(v)) for t, v in pairs]
    return sorted(typed_pairs, key=lambda p: p[0])


def _bucket_key(ts_ms: int, period: str, tz: ZoneInfo) -> tuple:
    """Return a period-bucket key in the given timezone.

    - ``day``   -> ``(year, month, day)``
    - ``week``  -> ``(iso_year, iso_week)``
    - ``month`` -> ``(year, month)``

    Raises:
        ValueError: when ``period`` is not in :data:`ALLOWED_PERIODS`.
    """
    if period not in ALLOWED_PERIODS:
        raise ValueError(
            f"Unknown period: {period!r}. Allowed: {sorted(ALLOWED_PERIODS)}"
        )
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=tz)
    if period == "day":
        return (dt.year, dt.month, dt.day)
    if period == "week":
        iso = dt.isocalendar()
        return (iso.year, iso.week)
    # month
    return (dt.year, dt.month)


def _bucket_by_period(
    pairs: list[tuple[int, float]], period: str, tz: ZoneInfo
) -> dict[tuple, float]:
    """Sum recorded values per period-bucket. Returns ``{bucket_key: total}``."""
    out: dict[tuple, float] = {}
    for ts, val in pairs:
        key = _bucket_key(ts, period, tz)
        out[key] = out.get(key, 0.0) + float(val)
    return out


def _today_bucket(period: str, tz: ZoneInfo) -> tuple:
    """Bucket key for "now" in the given timezone."""
    now = datetime.now(tz=tz)
    return _bucket_key(int(now.timestamp() * 1000), period, tz)


def _prev_bucket(bucket: tuple, period: str) -> tuple:
    """Step one period back from ``bucket``.

    For weekly buckets, walks via Monday-of-week minus 7 days so ISO-year
    rollovers (e.g. 2026-W01 -> 2025-W52) are handled correctly.
    """
    if period == "day":
        y, m, d = bucket
        prev = date(y, m, d) - timedelta(days=1)
        return (prev.year, prev.month, prev.day)
    if period == "week":
        iso_year, iso_week = bucket
        monday = date.fromisocalendar(iso_year, iso_week, 1)
        prev_monday = monday - timedelta(days=7)
        prev_iso = prev_monday.isocalendar()
        return (prev_iso.year, prev_iso.week)
    if period == "month":
        y, m = bucket
        if m == 1:
            return (y - 1, 12)
        return (y, m - 1)
    raise ValueError(f"Unknown period: {period!r}")


def _compute_streak(
    bucket_sums: dict[tuple, float],
    period: str,
    target: float,
    today_bucket: tuple,
) -> dict:
    """Compute current streak, longest streak and last fulfilled bucket.

    Rules:
    - A bucket is *fulfilled* iff ``sum >= target``.
    - ``current_streak`` counts consecutive fulfilled buckets ending at
      today.  If today is **not yet** fulfilled the streak does not break —
      we step one period back and count from there.  Rationale: a habit not
      yet recorded today is normal mid-day behaviour and must not punish
      the user.
    - ``longest_streak`` is the longest run of consecutive fulfilled
      buckets anywhere in history.
    - ``last_fulfilled_bucket`` is the most recent fulfilled bucket
      (or ``None`` when there is none).
    """
    # current streak — start at today, allow one "today not yet done" step back
    cursor = today_bucket
    if bucket_sums.get(cursor, 0) < target:
        cursor = _prev_bucket(cursor, period)
    current = 0
    while bucket_sums.get(cursor, 0) >= target:
        current += 1
        cursor = _prev_bucket(cursor, period)

    # longest streak — walk buckets in chronological order
    if not bucket_sums:
        return {"current": 0, "longest": 0, "last_fulfilled_bucket": None}

    sorted_buckets = sorted(bucket_sums.keys())
    longest = 0
    run = 0
    last_fulfilled: tuple | None = None
    prev_fulfilled: tuple | None = None
    for b in sorted_buckets:
        if bucket_sums[b] >= target:
            if last_fulfilled is None or b > last_fulfilled:
                last_fulfilled = b
            if prev_fulfilled is not None and _prev_bucket(b, period) == prev_fulfilled:
                run += 1
            else:
                run = 1
            if run > longest:
                longest = run
            prev_fulfilled = b
        else:
            run = 0
            prev_fulfilled = None

    return {
        "current": current,
        "longest": longest,
        "last_fulfilled_bucket": last_fulfilled,
    }


def get_habit_streak_impl(
    api_client,
    habit_id: str,
    target_per_period: float | None = None,
    tz: ZoneInfo | None = None,
) -> dict:
    """IO shell: read the habit document, then delegate to pure helpers.

    Args:
        api_client: object exposing ``get_document(doc_id) -> dict``.
        habit_id: opaque CouchDB ``_id`` of the habit (same value as the
            ``habitId`` field returned by ``/api/habits``).
        target_per_period: optional override for the per-period target. When
            ``None`` the doc's ``target`` is used (defaulting to ``1`` if the
            doc has ``0``/missing/None — Marvin allows a "no-target" habit
            where any record counts as fulfilled).
        tz: timezone used for bucket assignment. Defaults to the system
            local timezone — tests should inject an explicit ``ZoneInfo``.

    Raises:
        ValueError: when the document is not a Habit or has an unsupported
            ``period`` value.

    Implementation note: uses ``get_document`` (REST ``/api/doc?id=…``),
    not ``get_habit`` (REST ``/api/habits/habit?id=…``) — the latter returns
    a reduced reminder-focused projection without ``db``/``title``/``target``
    and with ``period`` encoded as int.
    """
    if tz is None:
        tz = datetime.now().astimezone().tzinfo  # type: ignore[assignment]

    habit = api_client.get_document(habit_id)
    if habit.get("db") != "Habits":
        raise ValueError(
            f"Document {habit_id!r} is not a Habit (db={habit.get('db')!r})"
        )

    period = habit.get("period")
    if period not in ALLOWED_PERIODS:
        raise ValueError(
            f"Habit {habit_id} has unsupported period: {period!r}. "
            f"Allowed: {sorted(ALLOWED_PERIODS)}"
        )

    # Marvin allows target=0 (no specific count required). Treat 0/None/missing
    # as "any record counts" -> target=1.
    raw_target = (
        target_per_period if target_per_period is not None else habit.get("target")
    )
    target = float(raw_target) if raw_target else 1.0
    history = habit.get("history") or []
    pairs = _parse_history(history)
    buckets = _bucket_by_period(pairs, period, tz)  # type: ignore[arg-type]
    today_bucket = _today_bucket(period, tz)  # type: ignore[arg-type]
    streak = _compute_streak(buckets, period, target, today_bucket)

    today_value = buckets.get(today_bucket, 0.0)
    return {
        "habit_id": habit_id,
        "title": habit.get("title"),
        "period": period,
        "target": target,
        "record_type": habit.get("recordType"),
        "current_streak": streak["current"],
        "longest_streak": streak["longest"],
        "last_fulfilled_bucket": streak["last_fulfilled_bucket"],
        "today_fulfilled": today_value >= target,
        "today_value": today_value,
        "total_records": len(pairs),
    }
