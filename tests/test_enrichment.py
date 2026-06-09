"""Unit tests for enrich_via_couchdb."""

from unittest.mock import MagicMock

from amazing_marvin_mcp.enrichment import enrich_via_couchdb


def _rest(_id, **extra):
    return {"_id": _id, **extra}


def _full(_id, **extra):
    return {"_id": _id, "db": "Categories", "createdAt": 1, "updatedAt": 2, **extra}


def test_returns_rest_unchanged_when_no_couchdb():
    api = MagicMock()
    api.has_couchdb = False
    rest = [_rest("a"), _rest("b")]
    assert enrich_via_couchdb(api, rest, "Categories") == rest
    api.find_docs.assert_not_called()


def test_returns_empty_when_rest_empty():
    api = MagicMock()
    api.has_couchdb = True
    assert enrich_via_couchdb(api, [], "Categories") == []
    api.find_docs.assert_not_called()


def test_merges_full_doc_for_matching_id_full_wins_on_overlap():
    api = MagicMock()
    api.has_couchdb = True
    api.find_docs.return_value = [
        _full("a", title="Real", rank=10),
        _full("b", title="Other"),
    ]
    rest = [_rest("a", title="Stale", masterRank=5)]

    enriched = enrich_via_couchdb(api, rest, "Categories")

    api.find_docs.assert_called_once_with({"db": "Categories"}, limit=500)
    assert len(enriched) == 1
    e = enriched[0]
    # Full overrides REST on conflict (title)
    assert e["title"] == "Real"
    # Full-only fields land in result
    assert e["db"] == "Categories"
    assert e["createdAt"] == 1
    assert e["rank"] == 10
    # REST-only fields are preserved
    assert e["masterRank"] == 5


def test_passes_through_when_id_not_in_full():
    api = MagicMock()
    api.has_couchdb = True
    api.find_docs.return_value = [_full("a")]
    rest = [_rest("a"), _rest("ghost", title="Not in CouchDB")]

    enriched = enrich_via_couchdb(api, rest, "Categories")

    assert len(enriched) == 2
    assert enriched[1]["title"] == "Not in CouchDB"
    # Ghost item is returned exactly as REST gave it
    assert enriched[1] == {"_id": "ghost", "title": "Not in CouchDB"}


def test_uses_custom_id_key_for_lookup():
    api = MagicMock()
    api.has_couchdb = True
    api.find_docs.return_value = [_full("habit-1", title="Pushups")]
    rest = [{"habitId": "habit-1", "history": []}]

    enriched = enrich_via_couchdb(api, rest, "Habits", id_key="habitId")

    assert enriched[0]["title"] == "Pushups"
    assert enriched[0]["history"] == []
