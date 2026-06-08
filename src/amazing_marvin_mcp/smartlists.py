"""Pure-logic implementations for SmartList MCP tools.

SmartLists live in the CouchDB `SmartLists` database (note plural). System
SmartLists like Today/Backlog are NOT stored in CouchDB and are not handled
here.

Filter-Clauses are stored as top-level fields on a SmartList document.
Each clause field is either absent / `null` (clause disabled) or a dict
`{"op": <operator>, "val": <value>}` (some operators have no `val`).

The WRITABLE_* sets below act as a whitelist to prevent callers from
mutating internal meta fields (`_id`, `_rev`, `db`, `createdAt`,
`fieldUpdates`).
"""

import time
import uuid

SMART_LIST_DB = "SmartLists"

# Layout / behavior fields the caller may set on create/update.
WRITABLE_TOP_LEVEL = {
    "name",
    "groupBy",
    "sort",
    "limit",
    "oneRT",
    "removeRedundancies",
    "refill",
}

# Filter-Clause fields — each value is null (clause off) or
# {"op": "...", "val": ...} (some ops omit "val").
WRITABLE_CLAUSE_FIELDS = {
    "itemType",
    "recurring",
    "parentId",
    "goalId",
    "title",
    "hasTime",
    "created",
    "day",
    "dueDate",
    "endDate",
    "startDate",
    "pledgeDate",
    "firstScheduled",
    "procrastinationCount",
    "backburner",
    "isStarred",
    "isFrogged",
    "labelIds",
    "project",
    "nextStep",
    "planAhead",
    "timeEstimate",
    "timeBlock",
    "advanced",
}

WRITABLE_FIELDS = WRITABLE_TOP_LEVEL | WRITABLE_CLAUSE_FIELDS


def list_smart_lists_impl(api_client) -> list[dict]:
    """Read all user-defined SmartLists via CouchDB and project to a summary.

    Raises ValueError when CouchDB credentials are missing (propagated from
    api_client.find_docs). Caller in main.py converts to an error response.
    """
    docs = api_client.find_docs({"db": SMART_LIST_DB}, limit=500)
    return [
        {
            "id": d["_id"],
            "name": d.get("name"),
            "groupBy": d.get("groupBy"),
            "sort": d.get("sort"),
            "limit": d.get("limit"),
            "active_clauses": [
                k for k in WRITABLE_CLAUSE_FIELDS if d.get(k) is not None
            ],
        }
        for d in docs
    ]


def get_smart_list_impl(api_client, smart_list_id: str) -> dict:
    """Read one SmartList document. Hard-fail if doc.db != 'SmartLists'."""
    doc = api_client.get_document(smart_list_id)
    if doc.get("db") != SMART_LIST_DB:
        raise ValueError(
            f"Document {smart_list_id!r} is not a SmartList "
            f"(db={doc.get('db')!r}). Use get_document for non-SmartList docs."
        )
    return doc


def _validate_writable(fields: dict) -> None:
    invalid = set(fields) - WRITABLE_FIELDS
    if invalid:
        raise ValueError(
            f"Not writable: {sorted(invalid)}. "
            f"Allowed: {sorted(WRITABLE_FIELDS)}"
        )


def _validate_clause_shapes(fields: dict) -> None:
    """Validate that every Clause-Field value is either None or a valid clause dict.

    A valid clause dict must have a non-empty "op" key.
    Non-clause fields (e.g. name, sort, limit) are ignored.

    Raises ValueError for any clause field with an invalid shape.
    """
    for key, value in fields.items():
        if key not in WRITABLE_CLAUSE_FIELDS:
            continue
        if value is None:
            continue
        if isinstance(value, dict) and "op" in value and value["op"]:
            continue
        raise ValueError(
            f"Filter clause {key!r} must be None or a dict with non-empty 'op' field, "
            f"got: {value!r}"
        )


def create_smart_list_impl(api_client, name: str, **fields) -> dict:
    """Create a new SmartList. `name` required, other fields optional (whitelisted)."""
    if not name or not isinstance(name, str):
        raise ValueError("`name` must be a non-empty string")
    _validate_writable(fields)
    _validate_clause_shapes(fields)
    now_ms = int(time.time() * 1000)
    doc = {
        "_id": uuid.uuid4().hex,  # 32 hex chars, collision-free
        "db": SMART_LIST_DB,
        "name": name,
        "createdAt": now_ms,
        "updatedAt": now_ms,
        **fields,
    }
    return api_client.create_document(doc)


def update_smart_list_impl(api_client, smart_list_id: str, **changes) -> dict:
    """Update fields on an existing SmartList. Whitelist + db-type enforced.

    Passing no changes is valid — it triggers a refresh by bumping updatedAt.
    """
    _validate_writable(changes)
    _validate_clause_shapes(changes)
    # Inject updatedAt after whitelist validation (not caller-settable).
    changes["updatedAt"] = int(time.time() * 1000)
    # Safety: verify it's actually a SmartList before mutating
    existing = api_client.get_document(smart_list_id)
    if existing.get("db") != SMART_LIST_DB:
        raise ValueError(
            f"Document {smart_list_id!r} is not a SmartList "
            f"(db={existing.get('db')!r})."
        )
    return api_client.update_document(smart_list_id, changes)


def delete_smart_list_impl(api_client, smart_list_id: str) -> dict:
    """Delete a SmartList. Safety: read first, ensure db == 'SmartLists'."""
    existing = api_client.get_document(smart_list_id)
    if existing.get("db") != SMART_LIST_DB:
        raise ValueError(
            f"Document {smart_list_id!r} is not a SmartList "
            f"(db={existing.get('db')!r}). Refusing to delete."
        )
    return api_client.delete_document(smart_list_id)
