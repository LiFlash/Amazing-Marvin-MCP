"""Smoke tests for the SmartList impl layer (no API key, no network)."""

from unittest.mock import MagicMock, call

import pytest
import requests

from amazing_marvin_mcp.smartlists import (
    SMART_LIST_DB,
    _validate_clause_shapes,
    _validate_writable,
    create_smart_list_impl,
    delete_smart_list_impl,
    get_smart_list_impl,
    list_smart_lists_impl,
    update_smart_list_impl,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _smartlist_doc(**overrides):
    """Return a minimal valid SmartList document, with optional overrides."""
    base = {
        "_id": "sl1",
        "_rev": "1-abc",
        "db": SMART_LIST_DB,
        "name": "My List",
        "groupBy": None,
        "sort": None,
        "limit": 0,
        "fieldUpdates": {},
    }
    base.update(overrides)
    return base


def _http_error(status_code=404):
    """Return a requests.HTTPError with the given status code."""
    response = MagicMock()
    response.status_code = status_code
    err = requests.HTTPError(response=response)
    return err


# ---------------------------------------------------------------------------
# Original 8 tests (unchanged)
# ---------------------------------------------------------------------------


def test_list_smart_lists_queries_correct_db():
    """list_smart_lists_impl must call find_docs with db=SmartLists."""
    client = MagicMock()
    client.find_docs.return_value = [
        {
            "_id": "abc123",
            "name": "My List",
            "groupBy": None,
            "sort": None,
            "limit": 0,
            "itemType": {"op": "task"},
        }
    ]
    result = list_smart_lists_impl(client)
    client.find_docs.assert_called_once_with({"db": SMART_LIST_DB}, limit=500)
    assert len(result) == 1
    assert result[0]["id"] == "abc123"
    assert result[0]["name"] == "My List"
    assert "itemType" in result[0]["active_clauses"]


def test_validate_writable_allows_name_blocks_id():
    """_validate_writable: `name` ok, `_id` rejected."""
    _validate_writable({"name": "x"})  # must not raise
    with pytest.raises(ValueError, match="Not writable"):
        _validate_writable({"_id": "abc"})


def test_get_smart_list_raises_when_doc_is_habit():
    """get_smart_list_impl must refuse a Habits doc."""
    client = MagicMock()
    client.get_document.return_value = {"_id": "h1", "db": "Habits"}
    with pytest.raises(ValueError, match="not a SmartList"):
        get_smart_list_impl(client, "h1")


def test_delete_smart_list_raises_and_does_not_delete_for_non_smartlist():
    """delete_smart_list_impl must NOT call delete_document for non-SmartList docs."""
    client = MagicMock()
    client.get_document.return_value = {"_id": "h1", "db": "Habits"}
    with pytest.raises(ValueError, match="not a SmartList"):
        delete_smart_list_impl(client, "h1")
    client.delete_document.assert_not_called()


def test_update_smart_list_empty_changes_calls_update_with_updated_at():
    """update_smart_list_impl with no changes must still call update_document with updatedAt."""
    client = MagicMock()
    client.get_document.return_value = {"_id": "sl1", "db": SMART_LIST_DB}
    client.update_document.return_value = {"_id": "sl1", "db": SMART_LIST_DB}
    update_smart_list_impl(client, "sl1")
    args, _ = client.update_document.call_args
    _id_arg, setters_arg = args
    assert _id_arg == "sl1"
    assert "updatedAt" in setters_arg
    assert isinstance(setters_arg["updatedAt"], int)


def test_update_smart_list_injects_updated_at():
    """update_smart_list_impl always bumps updatedAt even when other changes are given."""
    client = MagicMock()
    client.get_document.return_value = {"_id": "sl1", "db": SMART_LIST_DB}
    client.update_document.return_value = {"_id": "sl1"}
    update_smart_list_impl(client, "sl1", name="New Name")
    args, _ = client.update_document.call_args
    _id_arg, setters_arg = args
    assert setters_arg["name"] == "New Name"
    assert "updatedAt" in setters_arg


def test_validate_clause_shapes_valid_cases():
    """_validate_clause_shapes must accept None, valid clause dicts, and non-clause keys."""
    # All of these should not raise.
    _validate_clause_shapes({"name": "X"})  # non-clause key — ignored
    _validate_clause_shapes({"itemType": None})  # delete clause
    _validate_clause_shapes({"itemType": {"op": "task"}})  # op without val
    _validate_clause_shapes({"parentId": {"op": "in", "val": "abc"}})  # op + val
    _validate_clause_shapes({"planAhead": {"op": "&thisWeek"}})


def test_validate_clause_shapes_invalid_cases():
    """_validate_clause_shapes must reject wrong shapes for clause fields."""
    with pytest.raises(ValueError, match="itemType"):
        _validate_clause_shapes({"itemType": "task"})  # plain string
    with pytest.raises(ValueError, match="parentId"):
        _validate_clause_shapes({"parentId": {}})  # dict without op
    with pytest.raises(ValueError, match="itemType"):
        _validate_clause_shapes({"itemType": {"op": ""}})  # empty op
    with pytest.raises(ValueError, match="parentId"):
        _validate_clause_shapes({"parentId": {"val": "x"}})  # no op key


# ---------------------------------------------------------------------------
# B.1 list_smart_lists
# ---------------------------------------------------------------------------


def test_list_smart_lists_empty_returns_empty():
    """find_docs returning [] -> impl returns []."""
    client = MagicMock()
    client.find_docs.return_value = []
    result = list_smart_lists_impl(client)
    assert result == []


def test_list_smart_lists_summary_strips_metadata():
    """Summary must not contain _rev or fieldUpdates; must contain id, name, groupBy, sort, limit, active_clauses."""
    client = MagicMock()
    client.find_docs.return_value = [_smartlist_doc()]
    result = list_smart_lists_impl(client)
    assert len(result) == 1
    summary = result[0]
    assert "_rev" not in summary
    assert "fieldUpdates" not in summary
    assert "db" not in summary
    for key in ("id", "name", "groupBy", "sort", "limit", "active_clauses"):
        assert key in summary, f"Expected key {key!r} in summary"


def test_list_smart_lists_active_clauses_lists_only_non_null():
    """active_clauses must contain parentId (non-null) and NOT goalId (null)."""
    client = MagicMock()
    client.find_docs.return_value = [
        _smartlist_doc(parentId={"op": "in", "val": "abc"}, goalId=None)
    ]
    result = list_smart_lists_impl(client)
    active = result[0]["active_clauses"]
    assert "parentId" in active
    assert "goalId" not in active


def test_list_smart_lists_propagates_value_error_when_no_couchdb():
    """find_docs raising ValueError must propagate out of list_smart_lists_impl."""
    client = MagicMock()
    client.find_docs.side_effect = ValueError("AMAZING_MARVIN_DB_URI not set")
    with pytest.raises(ValueError, match="AMAZING_MARVIN_DB_URI"):
        list_smart_lists_impl(client)


# ---------------------------------------------------------------------------
# B.2 get_smart_list
# ---------------------------------------------------------------------------


def test_get_smart_list_returns_full_doc_for_smartlist():
    """get_smart_list_impl returns the exact dict from get_document."""
    doc = _smartlist_doc()
    client = MagicMock()
    client.get_document.return_value = doc
    result = get_smart_list_impl(client, "sl1")
    assert result is doc


def test_get_smart_list_propagates_http_error():
    """get_document raising HTTPError must propagate without being swallowed."""
    client = MagicMock()
    client.get_document.side_effect = _http_error(404)
    with pytest.raises(requests.HTTPError):
        get_smart_list_impl(client, "sl1")


# ---------------------------------------------------------------------------
# B.3 create_smart_list
# ---------------------------------------------------------------------------


def test_create_smart_list_requires_name():
    """name='' or name=None must raise ValueError."""
    client = MagicMock()
    with pytest.raises(ValueError):
        create_smart_list_impl(client, "")
    with pytest.raises(ValueError):
        create_smart_list_impl(client, None)
    client.create_document.assert_not_called()


def test_create_smart_list_name_must_be_string():
    """name=123 (not a str) must raise ValueError."""
    client = MagicMock()
    with pytest.raises(ValueError):
        create_smart_list_impl(client, 123)
    client.create_document.assert_not_called()


def test_create_smart_list_minimal_only_name():
    """With only name provided, create_document is called with a properly shaped doc."""
    client = MagicMock()
    client.create_document.return_value = {"_id": "new1", "db": SMART_LIST_DB}
    create_smart_list_impl(client, "My New List")
    client.create_document.assert_called_once()
    doc = client.create_document.call_args[0][0]
    # _id must be a 32-char hex string
    assert isinstance(doc["_id"], str)
    assert len(doc["_id"]) == 32
    assert all(c in "0123456789abcdef" for c in doc["_id"])
    assert doc["db"] == SMART_LIST_DB
    assert doc["name"] == "My New List"
    assert isinstance(doc["createdAt"], int)
    assert isinstance(doc["updatedAt"], int)


def test_create_smart_list_full_fields():
    """sort and limit in fields land in the created doc."""
    client = MagicMock()
    client.create_document.return_value = {}
    create_smart_list_impl(
        client,
        "Full List",
        sort=[{"field": "day", "dir": "asc"}],
        limit=10,
    )
    doc = client.create_document.call_args[0][0]
    assert doc["sort"] == [{"field": "day", "dir": "asc"}]
    assert doc["limit"] == 10


def test_create_smart_list_with_clause_in_op():
    """parentId clause with op=in and val is accepted and included in doc."""
    client = MagicMock()
    client.create_document.return_value = {}
    create_smart_list_impl(client, "List", parentId={"op": "in", "val": "abc"})
    doc = client.create_document.call_args[0][0]
    assert doc["parentId"] == {"op": "in", "val": "abc"}


def test_create_smart_list_with_clause_no_val_op_only():
    """itemType clause with op only (no val) is accepted."""
    client = MagicMock()
    client.create_document.return_value = {}
    create_smart_list_impl(client, "List", itemType={"op": "task"})
    doc = client.create_document.call_args[0][0]
    assert doc["itemType"] == {"op": "task"}


def test_create_smart_list_with_advanced_rpn():
    """advanced clause with op=y and RPN val is accepted without parsing."""
    client = MagicMock()
    client.create_document.return_value = {}
    rpn = "*hasChildren *false == *type:project &&"
    create_smart_list_impl(client, "List", advanced={"op": "y", "val": rpn})
    doc = client.create_document.call_args[0][0]
    assert doc["advanced"] == {"op": "y", "val": rpn}


def test_create_smart_list_blocks_non_whitelisted_field():
    """fields={"_id": "forced"} must raise ValueError and not call create_document."""
    client = MagicMock()
    with pytest.raises(ValueError, match="_id"):
        create_smart_list_impl(client, "List", _id="forced")
    client.create_document.assert_not_called()


def test_create_smart_list_blocks_metadata_fields():
    """_rev and fieldUpdates are not in the whitelist and must raise ValueError."""
    client = MagicMock()
    with pytest.raises(ValueError):
        create_smart_list_impl(client, "List", _rev="x")
    client.create_document.assert_not_called()

    with pytest.raises(ValueError):
        create_smart_list_impl(client, "List", fieldUpdates={})
    client.create_document.assert_not_called()


def test_create_smart_list_blocks_invalid_clause_shape():
    """itemType with a plain string value (not a dict) must raise ValueError naming the field."""
    client = MagicMock()
    with pytest.raises(ValueError, match="itemType"):
        create_smart_list_impl(client, "List", itemType="task")
    client.create_document.assert_not_called()


# ---------------------------------------------------------------------------
# B.4 update_smart_list
# ---------------------------------------------------------------------------


def test_update_smart_list_calls_get_then_update():
    """get_document must be called before update_document."""
    client = MagicMock()
    client.get_document.return_value = {"_id": "sl1", "db": SMART_LIST_DB}
    client.update_document.return_value = {"_id": "sl1"}
    update_smart_list_impl(client, "sl1", name="Changed")
    # Verify ordering: get_document before update_document
    call_names = [c[0] for c in client.method_calls]
    get_idx = call_names.index("get_document")
    update_idx = call_names.index("update_document")
    assert get_idx < update_idx


def test_update_smart_list_propagates_safety_check():
    """get_document returning a Habits doc raises ValueError and update_document is not called."""
    client = MagicMock()
    client.get_document.return_value = {"_id": "h1", "db": "Habits"}
    with pytest.raises(ValueError):
        update_smart_list_impl(client, "h1", name="x")
    client.update_document.assert_not_called()


def test_update_smart_list_blocks_non_whitelisted():
    """changes={"_id": "x"} must raise ValueError before any API call."""
    client = MagicMock()
    with pytest.raises(ValueError):
        update_smart_list_impl(client, "sl1", _id="x")
    client.get_document.assert_not_called()
    client.update_document.assert_not_called()


def test_update_smart_list_blocks_metadata_in_changes():
    """createdAt is not in the whitelist and must raise ValueError."""
    client = MagicMock()
    with pytest.raises(ValueError):
        update_smart_list_impl(client, "sl1", createdAt=123)
    client.update_document.assert_not_called()


def test_update_smart_list_blocks_invalid_clause():
    """parentId={} (dict without op) must raise ValueError."""
    client = MagicMock()
    with pytest.raises(ValueError, match="parentId"):
        update_smart_list_impl(client, "sl1", parentId={})
    client.update_document.assert_not_called()


def test_update_smart_list_clears_clause_with_null():
    """changes={"goalId": None} is valid and update_document is called with goalId=None."""
    client = MagicMock()
    client.get_document.return_value = {"_id": "sl1", "db": SMART_LIST_DB}
    client.update_document.return_value = {"_id": "sl1"}
    update_smart_list_impl(client, "sl1", goalId=None)
    client.update_document.assert_called_once()
    args, _ = client.update_document.call_args
    _id_arg, setters_arg = args
    assert "goalId" in setters_arg
    assert setters_arg["goalId"] is None


# ---------------------------------------------------------------------------
# B.5 delete_smart_list
# ---------------------------------------------------------------------------


def test_delete_smart_list_happy_path():
    """delete_document is called with the correct ID and its return value is passed through."""
    mock_response = {"ok": True, "id": "sl1"}
    client = MagicMock()
    client.get_document.return_value = _smartlist_doc()
    client.delete_document.return_value = mock_response
    result = delete_smart_list_impl(client, "sl1")
    client.delete_document.assert_called_once_with("sl1")
    assert result is mock_response


def test_delete_smart_list_propagates_get_error():
    """get_document raising HTTPError propagates and delete_document is not called."""
    client = MagicMock()
    client.get_document.side_effect = _http_error(404)
    with pytest.raises(requests.HTTPError):
        delete_smart_list_impl(client, "sl1")
    client.delete_document.assert_not_called()


def test_delete_smart_list_propagates_delete_error():
    """get_document succeeds but delete_document raising HTTPError propagates."""
    client = MagicMock()
    client.get_document.return_value = _smartlist_doc()
    client.delete_document.side_effect = _http_error(500)
    with pytest.raises(requests.HTTPError):
        delete_smart_list_impl(client, "sl1")
