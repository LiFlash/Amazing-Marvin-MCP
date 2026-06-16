"""Unit tests for the goals impl layer (no API key, no network)."""

from unittest.mock import MagicMock

import pytest

from amazing_marvin_mcp.goals import (
    GOALS_DB,
    _compute_setup_status,
    get_enriched_goal,
    get_goal_tasks_impl,
    link_task_to_goal_impl,
    unlink_task_from_goal_impl,
    update_goal_impl,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _goal_doc(**overrides):
    base = {
        "_id": "goal1",
        "_rev": "1-abc",
        "db": GOALS_DB,
        "title": "Reach 10MRR",
        "status": "active",
        "sections": [
            {"_id": "secA", "title": "Foundation", "note": "Basics"},
            {"_id": "secB", "title": "Launch", "note": ""},
        ],
    }
    base.update(overrides)
    return base


def _task_doc(**overrides):
    base = {"_id": "task1", "_rev": "1-x", "db": "Tasks", "title": "Do thing"}
    base.update(overrides)
    return base


def _client_with_couchdb(goal=None, linked=None):
    client = MagicMock()
    client.has_couchdb = True
    if goal is not None:
        client.get_document.return_value = goal
    client.find_docs.return_value = linked or []
    return client


# ---------------------------------------------------------------------------
# _verify_goal / _verify_linkable (via the public funcs)
# ---------------------------------------------------------------------------


def test_get_enriched_goal_rejects_non_goal_doc():
    client = MagicMock()
    client.has_couchdb = True
    client.get_document.return_value = {"_id": "x", "db": "Tasks"}
    with pytest.raises(ValueError, match="is not a Goal"):
        get_enriched_goal(client, "x")


def test_get_enriched_goal_raises_when_missing():
    client = MagicMock()
    client.has_couchdb = True
    client.get_document.return_value = None
    with pytest.raises(ValueError, match="not found"):
        get_enriched_goal(client, "missing")


def test_link_task_rejects_non_linkable_db():
    client = MagicMock()
    client.has_couchdb = True
    # First call (verify goal) returns goal, second (verify item) returns SmartList
    client.get_document.side_effect = [_goal_doc(), {"_id": "sl", "db": "SmartLists"}]
    with pytest.raises(ValueError, match="only.*can be linked"):
        link_task_to_goal_impl(client, "sl", "goal1")


# ---------------------------------------------------------------------------
# get_enriched_goal — aggregation
# ---------------------------------------------------------------------------


def test_get_enriched_goal_groups_items_into_sections():
    goal = _goal_doc()
    linked = [
        _task_doc(_id="t1", g_in_goal1=True, g_sec_goal1="secA", g_rank_goal1=2),
        _task_doc(_id="t2", g_in_goal1=True, g_sec_goal1="secA", g_rank_goal1=1),
        _task_doc(_id="t3", g_in_goal1=True, g_sec_goal1="secB", done=True),
        _task_doc(_id="t4", g_in_goal1=True),  # unsectioned
    ]
    client = _client_with_couchdb(goal=goal, linked=linked)

    result = get_enriched_goal(client, "goal1")

    # Linked items aggregated
    assert result["linked_summary"]["total"] == 4
    assert result["linked_summary"]["tasks"] == 4
    assert result["linked_summary"]["done"] == 1
    assert result["linked_summary"]["open"] == 3

    # Sections expanded with items, sorted by rank
    sec_a = next(s for s in result["sections_with_items"] if s["_id"] == "secA")
    assert [i["_id"] for i in sec_a["items"]] == ["t2", "t1"]
    sec_b = next(s for s in result["sections_with_items"] if s["_id"] == "secB")
    assert [i["_id"] for i in sec_b["items"]] == ["t3"]

    # Unsectioned items collected
    assert [i["_id"] for i in result["unsectioned_items"]] == ["t4"]

    # Denormalization of section_id / rank
    t1 = next(i for i in result["linked_items"] if i["_id"] == "t1")
    assert t1["goal_section_id"] == "secA"
    assert t1["goal_rank"] == 2

    # find_docs was called with the right selector
    client.find_docs.assert_called_once_with({"g_in_goal1": True}, limit=500)


def test_get_enriched_goal_computes_progress_when_expected_tasks_set():
    goal = _goal_doc(expectedTasks=10)
    linked = [_task_doc(_id=f"t{i}", g_in_goal1=True, done=(i < 3)) for i in range(5)]
    client = _client_with_couchdb(goal=goal, linked=linked)

    result = get_enriched_goal(client, "goal1")

    assert result["progress"] == {"done": 3, "expected_tasks": 10, "ratio": 0.3}


def test_get_enriched_goal_skips_progress_without_expected_tasks():
    goal = _goal_doc()  # no expectedTasks
    client = _client_with_couchdb(goal=goal, linked=[])
    result = get_enriched_goal(client, "goal1")
    assert "progress" not in result


def test_get_enriched_goal_falls_back_when_no_couchdb():
    goal = _goal_doc()
    client = MagicMock()
    client.has_couchdb = False
    client.get_document.return_value = goal

    result = get_enriched_goal(client, "goal1")

    assert result["linked_items"] == []
    assert result["linked_summary"]["total"] == 0
    assert "linked_items_note" in result
    client.find_docs.assert_not_called()


def test_get_enriched_goal_handles_goal_without_sections():
    goal = _goal_doc(sections=None)
    linked = [_task_doc(_id="t1", g_in_goal1=True, g_sec_goal1="ghost")]
    client = _client_with_couchdb(goal=goal, linked=linked)

    result = get_enriched_goal(client, "goal1")

    assert result["sections_with_items"] == []
    # Section ID points nowhere, so item is unsectioned
    assert [i["_id"] for i in result["unsectioned_items"]] == ["t1"]


def test_get_enriched_goal_counts_projects_and_habits():
    goal = _goal_doc()
    linked = [
        {"_id": "t1", "db": "Tasks", "g_in_goal1": True},
        {"_id": "p1", "db": "Categories", "type": "project", "g_in_goal1": True},
        {"_id": "c1", "db": "Categories", "type": "category", "g_in_goal1": True},
        {"_id": "h1", "db": "Habits", "g_in_goal1": True},
    ]
    client = _client_with_couchdb(goal=goal, linked=linked)

    result = get_enriched_goal(client, "goal1")

    assert result["linked_summary"]["tasks"] == 1
    assert result["linked_summary"]["projects"] == 1  # category-typed excluded
    assert result["linked_summary"]["habits"] == 1
    assert result["linked_summary"]["total"] == 4


# ---------------------------------------------------------------------------
# get_goal_tasks_impl
# ---------------------------------------------------------------------------


def test_get_goal_tasks_uses_section_filter_and_sorts_by_rank():
    goal = _goal_doc()
    linked = [
        _task_doc(_id="t1", g_in_goal1=True, g_sec_goal1="secA", g_rank_goal1=3),
        _task_doc(_id="t2", g_in_goal1=True, g_sec_goal1="secA"),  # no rank
        _task_doc(_id="t3", g_in_goal1=True, g_sec_goal1="secA", g_rank_goal1=1),
    ]
    client = _client_with_couchdb(goal=goal, linked=linked)

    items = get_goal_tasks_impl(client, "goal1", section_id="secA")

    client.find_docs.assert_called_once_with(
        {"g_in_goal1": True, "g_sec_goal1": "secA"}, limit=500
    )
    assert [i["_id"] for i in items] == ["t3", "t1", "t2"]  # rank asc, None last


def test_get_goal_tasks_include_done_false_filters_done():
    goal = _goal_doc()
    linked = [
        _task_doc(_id="t1", g_in_goal1=True, done=True),
        _task_doc(_id="t2", g_in_goal1=True),
    ]
    client = _client_with_couchdb(goal=goal, linked=linked)

    items = get_goal_tasks_impl(client, "goal1", include_done=False)
    assert [i["_id"] for i in items] == ["t2"]


def test_get_goal_tasks_requires_couchdb():
    goal = _goal_doc()
    client = MagicMock()
    client.has_couchdb = False
    client.get_document.return_value = goal

    with pytest.raises(ValueError, match="CouchDB credentials required"):
        get_goal_tasks_impl(client, "goal1")


# ---------------------------------------------------------------------------
# link_task_to_goal_impl
# ---------------------------------------------------------------------------


def test_link_task_sets_g_in_only_when_no_section_or_rank():
    client = MagicMock()
    client.has_couchdb = True
    # First get_document = goal, second = task
    client.get_document.side_effect = [_goal_doc(), _task_doc()]

    link_task_to_goal_impl(client, "task1", "goal1")

    args, kwargs = client.update_document.call_args
    assert args[0] == "task1"
    setters = args[1]
    assert setters["g_in_goal1"] is True
    assert "g_sec_goal1" not in setters
    assert "g_rank_goal1" not in setters
    assert "updatedAt" in setters


def test_link_task_with_section_and_rank_sets_all_three_fields():
    client = MagicMock()
    client.has_couchdb = True
    client.get_document.side_effect = [_goal_doc(), _task_doc()]

    link_task_to_goal_impl(client, "task1", "goal1", section_id="secA", rank=5)

    args, _ = client.update_document.call_args
    setters = args[1]
    assert setters["g_in_goal1"] is True
    assert setters["g_sec_goal1"] == "secA"
    assert setters["g_rank_goal1"] == 5


def test_link_task_rejects_unknown_section():
    client = MagicMock()
    client.has_couchdb = True
    client.get_document.side_effect = [_goal_doc(), _task_doc()]

    with pytest.raises(ValueError, match="does not exist on goal"):
        link_task_to_goal_impl(client, "task1", "goal1", section_id="bogus")

    client.update_document.assert_not_called()


def test_link_task_accepts_project_db_categories():
    client = MagicMock()
    client.has_couchdb = True
    client.get_document.side_effect = [
        _goal_doc(),
        {"_id": "p1", "db": "Categories", "type": "project"},
    ]
    link_task_to_goal_impl(client, "p1", "goal1")
    client.update_document.assert_called_once()


def test_link_task_accepts_habit_db():
    client = MagicMock()
    client.has_couchdb = True
    client.get_document.side_effect = [_goal_doc(), {"_id": "h1", "db": "Habits"}]
    link_task_to_goal_impl(client, "h1", "goal1")
    client.update_document.assert_called_once()


# ---------------------------------------------------------------------------
# unlink_task_from_goal_impl
# ---------------------------------------------------------------------------


def test_unlink_task_sets_all_three_fields_to_none():
    client = MagicMock()
    client.has_couchdb = True
    client.get_document.side_effect = [_goal_doc(), _task_doc()]

    unlink_task_from_goal_impl(client, "task1", "goal1")

    args, _ = client.update_document.call_args
    setters = args[1]
    assert setters["g_in_goal1"] is None
    assert setters["g_sec_goal1"] is None
    assert setters["g_rank_goal1"] is None
    assert "updatedAt" in setters


def test_unlink_task_requires_valid_goal():
    client = MagicMock()
    client.has_couchdb = True
    client.get_document.return_value = {"_id": "x", "db": "Tasks"}  # not a goal

    with pytest.raises(ValueError, match="is not a Goal"):
        unlink_task_from_goal_impl(client, "task1", "x")

    client.update_document.assert_not_called()


# ---------------------------------------------------------------------------
# update_goal_impl — whitelist + validation
# ---------------------------------------------------------------------------


def _client_with_goal(goal=None):
    client = MagicMock()
    client.has_couchdb = True
    client.get_document.return_value = goal if goal is not None else _goal_doc()
    return client


def test_update_goal_requires_at_least_one_change():
    client = _client_with_goal()
    with pytest.raises(ValueError, match="at least one field"):
        update_goal_impl(client, "goal1")
    client.update_document.assert_not_called()


def test_update_goal_rejects_unknown_field():
    client = _client_with_goal()
    with pytest.raises(ValueError, match="not in update_goal whitelist"):
        update_goal_impl(client, "goal1", challenges=[])
    client.update_document.assert_not_called()


def test_update_goal_rejects_non_goal_doc():
    client = _client_with_goal({"_id": "x", "db": "Tasks"})
    with pytest.raises(ValueError, match="is not a Goal"):
        update_goal_impl(client, "x", status="active")
    client.update_document.assert_not_called()


def test_update_goal_validates_status_enum():
    client = _client_with_goal()
    with pytest.raises(ValueError, match=r"status='archived'.*allowed values"):
        update_goal_impl(client, "goal1", status="archived")
    client.update_document.assert_not_called()


def test_update_goal_validates_importance_range():
    client = _client_with_goal()
    with pytest.raises(ValueError, match=r"importance=6.*range \[1, 5\]"):
        update_goal_impl(client, "goal1", importance=6)
    with pytest.raises(ValueError, match=r"importance=0.*range \[1, 5\]"):
        update_goal_impl(client, "goal1", importance=0)


def test_update_goal_validates_difficulty_range():
    client = _client_with_goal()
    with pytest.raises(ValueError, match=r"difficulty=10"):
        update_goal_impl(client, "goal1", difficulty=10)


def test_update_goal_validates_expected_tasks_nonneg():
    client = _client_with_goal()
    with pytest.raises(ValueError, match="expectedTasks=-1 must be >= 0"):
        update_goal_impl(client, "goal1", expectedTasks=-1)


def test_update_goal_rejects_wrong_type():
    client = _client_with_goal()
    with pytest.raises(ValueError, match="status='active'|must be str"):
        update_goal_impl(client, "goal1", status=123)
    with pytest.raises(ValueError, match="hasEnd=1 must be bool"):
        update_goal_impl(client, "goal1", hasEnd=1)
    with pytest.raises(ValueError, match="importance='3' must be int"):
        update_goal_impl(client, "goal1", importance="3")


def test_update_goal_writes_setters_and_bumps_updated_at():
    client = _client_with_goal()
    update_goal_impl(client, "goal1", status="active", note="go go go", importance=4)

    args, _ = client.update_document.call_args
    assert args[0] == "goal1"
    setters = args[1]
    assert setters["status"] == "active"
    assert setters["note"] == "go go go"
    assert setters["importance"] == 4
    assert "updatedAt" in setters
    assert isinstance(setters["updatedAt"], int)


def test_update_goal_accepts_none_to_clear_field():
    """Passing None bypasses type checks (clear-semantics for /doc/update)."""
    client = _client_with_goal()
    # dueDate is str, but None must be accepted to clear
    update_goal_impl(client, "goal1", dueDate=None)
    args, _ = client.update_document.call_args
    assert args[1]["dueDate"] is None


# ---------------------------------------------------------------------------
# _compute_setup_status — UI checklist mapping
# ---------------------------------------------------------------------------


def test_setup_status_pending_goal_with_no_setup_is_all_false_except_title():
    goal = _goal_doc(status="pending")
    s = _compute_setup_status(goal, has_actions=False)
    assert s["has_title"] is True
    assert s["has_actions"] is False
    assert s["has_expectations"] is False
    assert s["has_checkins"] is False
    assert s["has_trackers"] is False
    assert s["is_committed"] is False
    assert "6. Commit (status != 'pending')" in s["missing_steps"]
    assert "3. Add Actions (Tasks/Projects/Habits)" in s["missing_steps"]


def test_setup_status_has_actions_reflects_linked_items():
    goal = _goal_doc()
    assert _compute_setup_status(goal, has_actions=True)["has_actions"] is True
    assert _compute_setup_status(goal, has_actions=False)["has_actions"] is False


def test_setup_status_committed_when_status_not_pending():
    for status in ("active", "done", "backburner"):
        s = _compute_setup_status(_goal_doc(status=status), has_actions=False)
        assert s["is_committed"] is True


def test_setup_status_expectations_detected_from_any_field():
    assert _compute_setup_status(
        _goal_doc(expectedTasks=5), has_actions=False
    )["has_expectations"] is True
    assert _compute_setup_status(
        _goal_doc(expectedDuration=120), has_actions=False
    )["has_expectations"] is True
    assert _compute_setup_status(
        _goal_doc(expectedHabits="A"), has_actions=False
    )["has_expectations"] is True
    # All zero/empty should still be False
    assert _compute_setup_status(
        _goal_doc(expectedTasks=0, expectedDuration=0, expectedHabits=""),
        has_actions=False,
    )["has_expectations"] is False


def test_setup_status_checkins_requires_checkin_enabled():
    assert _compute_setup_status(
        _goal_doc(checkIn=True), has_actions=False
    )["has_checkins"] is True
    assert _compute_setup_status(
        _goal_doc(checkIn=False), has_actions=False
    )["has_checkins"] is False


def test_setup_status_trackers_detected_via_trackerprogress_keys():
    goal = _goal_doc(**{"trackerProgress_abc123": True})
    assert _compute_setup_status(goal, has_actions=False)["has_trackers"] is True
    assert _compute_setup_status(_goal_doc(), has_actions=False)["has_trackers"] is False


def test_get_enriched_goal_includes_setup_status():
    """End-to-end: the enriched goal payload exposes setup_status."""
    goal = _goal_doc(status="active", expectedTasks=10, checkIn=True)
    linked = [_task_doc(_id="t1", g_in_goal1=True)]
    client = _client_with_couchdb(goal=goal, linked=linked)

    result = get_enriched_goal(client, "goal1")

    s = result["setup_status"]
    assert s["is_committed"] is True
    assert s["has_actions"] is True
    assert s["has_expectations"] is True
    assert s["has_checkins"] is True
