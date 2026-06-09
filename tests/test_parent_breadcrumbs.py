"""Unit tests for add_parent_breadcrumbs."""

from unittest.mock import MagicMock

from amazing_marvin_mcp.enrichment import add_parent_breadcrumbs


def _cat(_id, title, parentId=None):
    return {"_id": _id, "title": title, "parentId": parentId, "db": "Categories"}


def _task(_id, title, parentId=None):
    return {"_id": _id, "title": title, "parentId": parentId, "db": "Tasks"}


def test_assigns_empty_breadcrumb_for_unassigned():
    items = [_task("t1", "X", parentId="unassigned")]
    add_parent_breadcrumbs(MagicMock(has_couchdb=False, get_categories=MagicMock(return_value=[])), items)
    assert items[0]["parent_path"] == []
    assert items[0]["parent_title"] is None


def test_assigns_empty_for_missing_parent_field():
    items = [_task("t1", "X", parentId=None)]
    add_parent_breadcrumbs(MagicMock(has_couchdb=False, get_categories=MagicMock(return_value=[])), items)
    assert items[0]["parent_path"] == []
    assert items[0]["parent_title"] is None


def test_walks_two_levels():
    api = MagicMock()
    api.has_couchdb = True
    api.find_docs.return_value = [
        _cat("root", "Work"),
        _cat("mid", "Customer X", parentId="root"),
    ]
    items = [_task("t1", "Bug", parentId="mid")]
    add_parent_breadcrumbs(api, items)
    assert items[0]["parent_path"] == ["Work", "Customer X"]
    assert items[0]["parent_title"] == "Customer X"


def test_uses_passed_category_map_without_extra_calls():
    api = MagicMock()
    cat_map = {"p": _cat("p", "Proj")}
    items = [_task("t1", "X", parentId="p")]
    add_parent_breadcrumbs(api, items, category_map=cat_map)
    api.find_docs.assert_not_called()
    api.get_categories.assert_not_called()
    assert items[0]["parent_path"] == ["Proj"]


def test_skips_unknown_parent_id():
    api = MagicMock()
    api.has_couchdb = False
    api.get_categories.return_value = [_cat("known", "Work")]
    items = [_task("t1", "X", parentId="ghost")]
    add_parent_breadcrumbs(api, items)
    assert items[0]["parent_path"] == []
    assert items[0]["parent_title"] is None


def test_falls_back_to_get_categories_when_no_couchdb():
    api = MagicMock()
    api.has_couchdb = False
    api.get_categories.return_value = [_cat("p", "Active")]
    items = [_task("t1", "X", parentId="p")]
    add_parent_breadcrumbs(api, items)
    api.find_docs.assert_not_called()
    api.get_categories.assert_called_once()
    assert items[0]["parent_title"] == "Active"


def test_uses_find_docs_when_couchdb_available():
    api = MagicMock()
    api.has_couchdb = True
    api.find_docs.return_value = [_cat("p", "Done", parentId=None)]
    items = [_task("t1", "X", parentId="p")]
    add_parent_breadcrumbs(api, items)
    api.find_docs.assert_called_once_with({"db": "Categories"}, limit=500)
    api.get_categories.assert_not_called()
    assert items[0]["parent_title"] == "Done"


def test_defends_against_cycle():
    api = MagicMock()
    api.has_couchdb = True
    # a points to b, b points to a — degenerate cycle
    api.find_docs.return_value = [
        _cat("a", "A", parentId="b"),
        _cat("b", "B", parentId="a"),
    ]
    items = [_task("t1", "X", parentId="a")]
    add_parent_breadcrumbs(api, items)
    # Doesn't loop forever; returns a finite path
    assert items[0]["parent_path"] == ["B", "A"]


def test_empty_items_short_circuits():
    api = MagicMock()
    result = add_parent_breadcrumbs(api, [])
    assert result == []
    api.find_docs.assert_not_called()
    api.get_categories.assert_not_called()


def test_mutates_in_place_and_returns_same_list():
    items = [_task("t1", "X")]
    out = add_parent_breadcrumbs(
        MagicMock(has_couchdb=False, get_categories=MagicMock(return_value=[])),
        items,
    )
    assert out is items
    assert "parent_path" in items[0]
