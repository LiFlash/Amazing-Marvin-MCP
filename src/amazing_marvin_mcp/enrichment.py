"""Generic enrichment helpers for REST endpoints that return reduced
projections.

Several Marvin REST list endpoints (``/api/categories``, ``/api/habits``,
…) return documents stripped of fields that the consuming LLM needs:
``createdAt``, ``updatedAt``, ``db``, ``done``, ``rank``, etc.

When CouchDB direct access is configured, one Mango ``_find`` query
returns the full docs and we replace the projection with the canonical
shape. Without CouchDB the cost of an N+1 ``/api/doc?id=`` sweep can
dominate for collections with dozens of items, so we keep the REST
projection in that case and let callers opt into ``get_document`` when
they really need a specific field.
"""

from __future__ import annotations


def enrich_via_couchdb(
    api_client,
    rest_items: list[dict],
    db_name: str,
    *,
    id_key: str = "_id",
) -> list[dict]:
    """Return the full CouchDB docs for items in ``rest_items`` when
    CouchDB direct access is available.

    The merge preserves any REST-only fields (rare, but e.g. habits'
    ``habitId``) by layering ``full | rest`` from the REST item on top
    of the find_docs result.

    When ``has_couchdb`` is False, returns ``rest_items`` unchanged.
    """
    if not getattr(api_client, "has_couchdb", False):
        return rest_items
    if not rest_items:
        return []

    full_docs = api_client.find_docs({"db": db_name}, limit=500)
    by_id = {d["_id"]: d for d in full_docs if d.get("_id")}

    enriched: list[dict] = []
    for item in rest_items:
        iid = item.get(id_key) or item.get("_id")
        if iid and iid in by_id:
            # Full doc wins for overlapping keys (e.g. period as string,
            # not int). REST-only keys (habitId, nextReminder, ...) are
            # preserved on top.
            enriched.append({**by_id[iid], **{k: v for k, v in item.items()
                                              if k not in by_id[iid]}})
        else:
            enriched.append(item)
    return enriched
