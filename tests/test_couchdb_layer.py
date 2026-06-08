"""Unit tests for the CouchDB layer in MarvinAPIClient (has_couchdb, find_docs).

All tests use unittest.mock — no live network calls.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

from amazing_marvin_mcp.api import MarvinAPIClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _client(
    db_uri: str = "https://x.cloudant.com",
    db_name: str = "db",
    db_user: str = "u",
    db_password: str = "p",
) -> MarvinAPIClient:
    return MarvinAPIClient(
        api_key="k",
        db_uri=db_uri,
        db_name=db_name,
        db_user=db_user,
        db_password=db_password,
    )


def _mock_post_ok(docs: list | None = None):
    """Return a mock requests.Response that succeeds with the given docs list."""
    if docs is None:
        docs = []
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"docs": docs}
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


# ---------------------------------------------------------------------------
# A.1 has_couchdb property (7 cases)
# ---------------------------------------------------------------------------

def test_has_couchdb_true_when_all_four_settings_present():
    """Case 1: all four db_* set -> True."""
    assert _client().has_couchdb is True


def test_has_couchdb_false_when_uri_missing():
    """Case 2: db_uri empty -> False."""
    assert _client(db_uri="").has_couchdb is False


def test_has_couchdb_false_when_name_missing():
    """Case 3: db_name empty -> False."""
    assert _client(db_name="").has_couchdb is False


def test_has_couchdb_false_when_user_missing():
    """Case 4: db_user empty -> False.

    CouchDB without auth is not supported — documented design decision.
    """
    assert _client(db_user="").has_couchdb is False


def test_has_couchdb_false_when_password_missing():
    """Case 5: db_password empty -> False."""
    assert _client(db_password="").has_couchdb is False


def test_has_couchdb_false_when_no_settings_present():
    """Case 6: no db_* settings at all (default constructor) -> False."""
    c = MarvinAPIClient(api_key="k")
    assert c.has_couchdb is False


@pytest.mark.xfail(
    reason=(
        "The current implementation uses bool() which treats whitespace-only strings as "
        "truthy. A whitespace-only credential would be accepted but is almost certainly "
        "wrong. Stripping before the bool check (e.g. bool(x.strip())) would be the "
        "correct behaviour. Mark as xfail to document the gap; change to a normal assert "
        "once strip() is applied."
    ),
    strict=True,
)
def test_has_couchdb_false_when_whitespace_only_value():
    """Case 7: whitespace-only db_password -> should be False.

    Current behaviour: bool('   ') is True, so has_couchdb returns True.
    Desired behaviour: False (empty-equivalent).
    This test is marked xfail to document the discrepancy.
    """
    c = _client(db_password="   ")
    # This assertion reflects the *desired* behaviour.
    assert c.has_couchdb is False


# ---------------------------------------------------------------------------
# A.2 find_docs — guard (case 8)
# ---------------------------------------------------------------------------

def test_find_docs_raises_value_error_when_not_configured():
    """Case 8: ValueError when CouchDB is not configured; message contains all four ENV names."""
    c = MarvinAPIClient(api_key="k")  # no db_* settings
    with pytest.raises(ValueError) as exc_info:
        c.find_docs({"db": "SmartLists"})
    msg = str(exc_info.value)
    for env_name in ["AMAZING_MARVIN_DB_URI", "AMAZING_MARVIN_DB_NAME",
                     "AMAZING_MARVIN_DB_USER", "AMAZING_MARVIN_DB_PASSWORD"]:
        assert env_name in msg, f"Expected '{env_name}' in ValueError message, got: {msg!r}"


# ---------------------------------------------------------------------------
# A.2 find_docs — URL construction (cases 9, 10)
# ---------------------------------------------------------------------------

@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_calls_correct_url(mock_post):
    """Case 9: requests.post is called with {db_uri}/{db_name}/_find (no double slash)."""
    mock_post.return_value = _mock_post_ok()
    _client().find_docs({"db": "SmartLists"})
    url_called = mock_post.call_args[0][0]
    assert url_called == "https://x.cloudant.com/db/_find"


@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_trailing_slash_in_uri_no_double_slash(mock_post):
    """Case 10: trailing slash in db_uri must not produce a double slash in the URL."""
    mock_post.return_value = _mock_post_ok()
    c = _client(db_uri="https://x.cloudant.com/")
    c.find_docs({"db": "SmartLists"})
    url_called = mock_post.call_args[0][0]
    assert "//" not in url_called.replace("https://", ""), (
        f"Double slash found in URL: {url_called!r}"
    )
    assert url_called == "https://x.cloudant.com/db/_find"


# ---------------------------------------------------------------------------
# A.2 find_docs — auth (case 11)
# ---------------------------------------------------------------------------

@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_passes_basic_auth_tuple(mock_post):
    """Case 11: requests.post receives auth=(user, password)."""
    mock_post.return_value = _mock_post_ok()
    _client(db_user="myuser", db_password="mypassword").find_docs({"db": "SmartLists"})
    _, kwargs = mock_post.call_args
    assert kwargs.get("auth") == ("myuser", "mypassword")


# ---------------------------------------------------------------------------
# A.2 find_docs — request body (cases 12, 13, 14)
# ---------------------------------------------------------------------------

@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_body_without_fields_when_fields_none(mock_post):
    """Case 12: body is {selector, limit} and does NOT contain 'fields' when fields=None."""
    mock_post.return_value = _mock_post_ok()
    selector = {"db": "SmartLists"}
    _client().find_docs(selector, fields=None)
    _, kwargs = mock_post.call_args
    body = kwargs.get("json")
    assert body is not None
    assert body["selector"] == selector
    assert body["limit"] == 500
    assert "fields" not in body, "fields key must be absent when fields=None"


@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_body_with_fields_when_fields_provided(mock_post):
    """Case 13: body contains 'fields' key when fields=[...] is passed."""
    mock_post.return_value = _mock_post_ok()
    _client().find_docs({"db": "SmartLists"}, fields=["_id", "name"])
    _, kwargs = mock_post.call_args
    body = kwargs.get("json")
    assert body["fields"] == ["_id", "name"]


@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_default_limit_is_500(mock_post):
    """Case 14: default limit sent in body is 500."""
    mock_post.return_value = _mock_post_ok()
    _client().find_docs({"db": "SmartLists"})
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["limit"] == 500


# ---------------------------------------------------------------------------
# A.2 find_docs — return value (cases 15, 15b)
# ---------------------------------------------------------------------------

@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_returns_docs_array(mock_post):
    """Case 15: find_docs returns the docs list, not the wrapper object."""
    docs = [{"_id": "abc", "name": "List A"}, {"_id": "def", "name": "List B"}]
    mock_post.return_value = _mock_post_ok(docs=docs)
    result = _client().find_docs({"db": "SmartLists"})
    assert result == docs


@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_returns_empty_list_on_empty_docs(mock_post):
    """Case 15b (from spec A.2): empty docs array -> [] returned, no crash."""
    mock_post.return_value = _mock_post_ok(docs=[])
    result = _client().find_docs({"db": "SmartLists"})
    assert result == []


# ---------------------------------------------------------------------------
# A.2 find_docs — custom limit (spec A.2 find_docs_custom_limit_overrides_default)
# ---------------------------------------------------------------------------

@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_custom_limit_overrides_default(mock_post):
    """Custom limit is forwarded in the body, not the default 500."""
    mock_post.return_value = _mock_post_ok()
    _client().find_docs({"db": "SmartLists"}, limit=10)
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["limit"] == 10


# ---------------------------------------------------------------------------
# A.2 find_docs — selector not mutated (spec A.2 find_docs_does_not_mutate_selector_arg)
# ---------------------------------------------------------------------------

@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_does_not_mutate_selector_arg(mock_post):
    """find_docs must not modify the caller's selector dict."""
    mock_post.return_value = _mock_post_ok()
    selector = {"db": "SmartLists"}
    original_selector = selector.copy()
    _client().find_docs(selector)
    assert selector == original_selector


# ---------------------------------------------------------------------------
# A.2 find_docs — db-less selector passed through (spec A.4)
# ---------------------------------------------------------------------------

@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_passes_through_db_less_selector(mock_post):
    """Selector without 'db' key is forwarded unchanged — no magic in find_docs."""
    mock_post.return_value = _mock_post_ok()
    selector = {"name": {"$eq": "test"}}
    _client().find_docs(selector)
    _, kwargs = mock_post.call_args
    assert kwargs["json"]["selector"] == {"name": {"$eq": "test"}}


# ---------------------------------------------------------------------------
# A.3 find_docs — error propagation (cases 16, 401/500, connection error)
# ---------------------------------------------------------------------------

@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_propagates_http_error_on_401(mock_post):
    """Case 16: HTTPError from raise_for_status() is not swallowed (401 case)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    http_error = requests.exceptions.HTTPError(response=mock_resp)
    mock_resp.raise_for_status.side_effect = http_error
    mock_post.return_value = mock_resp

    with pytest.raises(requests.exceptions.HTTPError):
        _client().find_docs({"db": "SmartLists"})


@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_propagates_http_error_on_500(mock_post):
    """HTTPError from raise_for_status() is not swallowed (500 case)."""
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    http_error = requests.exceptions.HTTPError(response=mock_resp)
    mock_resp.raise_for_status.side_effect = http_error
    mock_post.return_value = mock_resp

    with pytest.raises(requests.exceptions.HTTPError):
        _client().find_docs({"db": "SmartLists"})


@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_propagates_connection_error(mock_post):
    """ConnectionError from requests is propagated, not swallowed."""
    mock_post.side_effect = requests.exceptions.ConnectionError("unreachable")

    with pytest.raises(requests.exceptions.ConnectionError):
        _client().find_docs({"db": "SmartLists"})


# ---------------------------------------------------------------------------
# A.5 find_docs — special characters in password (case 17)
# ---------------------------------------------------------------------------

@patch("amazing_marvin_mcp.api.requests.post")
def test_find_docs_password_with_special_chars_passed_in_auth_tuple(mock_post):
    """Case 17: password with special chars is forwarded verbatim in the auth tuple.

    requests handles encoding internally when using auth=(...) — no manual
    URL-encoding needed. This test verifies that the raw password reaches the
    auth parameter unchanged.
    """
    mock_post.return_value = _mock_post_ok()
    special_password = "p@ss/w:rd"
    _client(db_user="user", db_password=special_password).find_docs({"db": "SmartLists"})
    _, kwargs = mock_post.call_args
    assert kwargs["auth"] == ("user", special_password)
