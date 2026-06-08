"""Unit tests for get_enriched_habits / get_enriched_habit.

Marvin's /api/habits and /api/habit endpoints return a reduced
projection (no `db`, no `title`, no `target`, `period` as int code).
The enrichment helpers must fall back to /api/doc?id= per habit when
CouchDB direct access is not configured, and use Mango _find for one
round-trip when it is.
"""

from unittest.mock import MagicMock

from amazing_marvin_mcp.habits import (
    HABIT_DB,
    get_enriched_habit,
    get_enriched_habits,
)


def _full_doc(_id="h1", title="Push-ups", period="day", target=1):
    return {
        "_id": _id,
        "db": HABIT_DB,
        "title": title,
        "period": period,
        "target": target,
        "recordType": "boolean",
        "history": [],
    }


def _rest_projection(habit_id="h1"):
    """Mimic Marvin's reduced /api/habits projection."""
    return {
        "userId": "u1",
        "habitId": habit_id,
        "history": [],
        "period": 0,  # int code in the projection
        "time": "08:00",
    }


def test_get_enriched_habits_uses_find_docs_when_couchdb_available():
    api = MagicMock()
    api.has_couchdb = True
    api.find_docs.return_value = [_full_doc("h1"), _full_doc("h2", title="Yoga")]

    result = get_enriched_habits(api)

    api.find_docs.assert_called_once_with({"db": HABIT_DB}, limit=500)
    api.get_habits.assert_not_called()
    api.get_document.assert_not_called()
    assert [h["_id"] for h in result] == ["h1", "h2"]
    assert all(h["db"] == "Habits" for h in result)


def test_get_enriched_habits_falls_back_to_rest_plus_get_document():
    api = MagicMock()
    api.has_couchdb = False
    api.get_habits.return_value = [
        _rest_projection("h1"),
        _rest_projection("h2"),
    ]
    api.get_document.side_effect = [
        _full_doc("h1", title="Push-ups"),
        _full_doc("h2", title="Yoga", period="week", target=3),
    ]

    result = get_enriched_habits(api)

    api.find_docs.assert_not_called()
    api.get_habits.assert_called_once()
    assert api.get_document.call_count == 2
    # Both titles and period-as-string are present after enrichment
    assert [h["title"] for h in result] == ["Push-ups", "Yoga"]
    assert [h["period"] for h in result] == ["day", "week"]
    # The REST projection's `history` and `time` fields are preserved on the merged doc
    assert all("history" in h for h in result)


def test_get_enriched_habits_falls_back_on_per_doc_lookup_failure():
    """If get_document fails for one habit, the others must still be returned."""
    api = MagicMock()
    api.has_couchdb = False
    api.get_habits.return_value = [
        _rest_projection("h1"),
        _rest_projection("h2"),
    ]
    api.get_document.side_effect = [
        _full_doc("h1", title="OK"),
        RuntimeError("boom"),
    ]

    result = get_enriched_habits(api)

    assert len(result) == 2
    assert result[0]["title"] == "OK"
    # Second falls back to the REST projection (no title) but keeps the _id
    assert result[1]["_id"] == "h2"


def test_get_enriched_habits_skips_entries_without_id():
    api = MagicMock()
    api.has_couchdb = False
    api.get_habits.return_value = [{"history": []}, _rest_projection("h2")]
    api.get_document.return_value = _full_doc("h2", title="Yoga")

    result = get_enriched_habits(api)

    # Only one habit survived (the one with an id)
    assert len(result) == 1
    assert result[0]["_id"] == "h2"


def test_get_enriched_habit_calls_get_document():
    api = MagicMock()
    api.get_document.return_value = _full_doc("h1", title="X")

    result = get_enriched_habit(api, "h1")

    api.get_document.assert_called_once_with("h1")
    assert result["title"] == "X"
    assert result["db"] == "Habits"
