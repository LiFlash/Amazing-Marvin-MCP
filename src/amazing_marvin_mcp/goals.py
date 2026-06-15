"""Goals helpers — Stufe 1 (read + Task↔Goal linking only).

Marvin's goal model (from MarvinAPI wiki — "Goals" data type):
  - `db`     = "Goals"
  - `status` in {"backburner", "pending", "active", "done"}
  - `sections[]` = goal's milestones / phases, each {_id, title, note}
  - rich strategic fields: motivations, challenges, expectedTasks,
    expectedDuration, checkIn*, importance, difficulty, …

Task/Project/Habit attachment is encoded on the item side via three
per-goal fields:
  - g_in_<GOAL_ID>  = true        — item belongs to this goal
  - g_sec_<GOAL_ID> = "<sec_id>"  — section/phase within the goal
  - g_rank_<GOAL_ID> = <int>      — ordering within section

This module exposes read-only helpers and Task-side linking only.
Direct Goal-doc mutation (create/update/delete, milestone CRUD) is NOT
implemented in this stage — it would write into Marvin's Goal docs,
which sync to all clients and could break the UI on malformed input.
"""

from __future__ import annotations

import time
from typing import Any

GOALS_DB = "Goals"
LINKABLE_DBS = {"Tasks", "Categories", "Habits"}


def _verify_goal(api_client, goal_id: str) -> dict:
    """Read the goal doc and raise if it isn't actually a Goal."""
    doc = api_client.get_document(goal_id)
    if not doc:
        raise ValueError(f"Goal {goal_id!r} not found.")
    if doc.get("db") != GOALS_DB:
        raise ValueError(
            f"Document {goal_id!r} is not a Goal (db={doc.get('db')!r})."
        )
    return doc


def _verify_linkable(api_client, item_id: str) -> dict:
    """Read item doc and raise unless it's a Task / Project / Habit."""
    doc = api_client.get_document(item_id)
    if not doc:
        raise ValueError(f"Item {item_id!r} not found.")
    db = doc.get("db")
    if db not in LINKABLE_DBS:
        raise ValueError(
            f"Item {item_id!r} has db={db!r}; only "
            f"{sorted(LINKABLE_DBS)} can be linked to a goal."
        )
    return doc


def get_enriched_goal(api_client, goal_id: str) -> dict:
    """Return the goal doc plus aggregated linked-item view + light progress.

    Adds the following synthesized fields on top of the raw goal:
      - ``linked_items``       — list of items with g_in_<goal_id>=true
                                 (each item gets ``goal_section_id`` and
                                 ``goal_rank`` denormalized for convenience).
      - ``linked_summary``     — counts of {tasks, projects, habits,
                                 done, open}.
      - ``sections_with_items`` — sections[] but each section dict gets
                                  an ``items`` list of linked items
                                  assigned to that section.
      - ``unsectioned_items``  — linked items with no g_sec_<goal_id>.
      - ``progress``           — {done, expected_tasks, ratio} when the
                                 goal sets ``expectedTasks``. Ratio is
                                 done_this_week_estimate is *not* computed
                                 here (Stufe 1 stays simple).

    Read-only. Requires CouchDB for ``linked_items`` aggregation; without
    CouchDB those fields are returned empty with ``linked_items_note``.
    """
    goal = _verify_goal(api_client, goal_id)

    linked: list[dict] = []
    note: str | None = None
    if getattr(api_client, "has_couchdb", False):
        flag = f"g_in_{goal_id}"
        linked = api_client.find_docs({flag: True}, limit=500)
        # Denormalize section/rank onto each item for downstream use.
        sec_key = f"g_sec_{goal_id}"
        rank_key = f"g_rank_{goal_id}"
        for it in linked:
            it["goal_section_id"] = it.get(sec_key)
            it["goal_rank"] = it.get(rank_key)
    else:
        note = (
            "CouchDB credentials not configured — cannot aggregate linked items. "
            "Set AMAZING_MARVIN_DB_* env vars or query a SmartList with a "
            "`goalId` clause instead."
        )

    sections = goal.get("sections") or []
    items_by_section: dict[str, list[dict]] = {s["_id"]: [] for s in sections if s.get("_id")}
    unsectioned: list[dict] = []
    for it in linked:
        sec_id = it.get("goal_section_id")
        if sec_id and sec_id in items_by_section:
            items_by_section[sec_id].append(it)
        else:
            unsectioned.append(it)

    sections_with_items = []
    for s in sections:
        sid = s.get("_id")
        s_copy = dict(s)
        s_copy["items"] = sorted(
            items_by_section.get(sid, []),
            key=lambda i: (i.get("goal_rank") is None, i.get("goal_rank") or 0),
        )
        sections_with_items.append(s_copy)

    done_count = sum(1 for i in linked if i.get("done"))
    summary = {
        "total": len(linked),
        "tasks": sum(1 for i in linked if i.get("db") == "Tasks"),
        "projects": sum(
            1 for i in linked
            if i.get("db") == "Categories" and i.get("type") == "project"
        ),
        "habits": sum(1 for i in linked if i.get("db") == "Habits"),
        "done": done_count,
        "open": len(linked) - done_count,
    }

    enriched = dict(goal)
    enriched["linked_items"] = linked
    enriched["linked_summary"] = summary
    enriched["sections_with_items"] = sections_with_items
    enriched["unsectioned_items"] = unsectioned
    if note:
        enriched["linked_items_note"] = note

    expected = goal.get("expectedTasks")
    if isinstance(expected, (int, float)) and expected > 0:
        enriched["progress"] = {
            "done": done_count,
            "expected_tasks": expected,
            "ratio": round(done_count / expected, 3),
        }

    return enriched


def get_goal_tasks_impl(
    api_client,
    goal_id: str,
    section_id: str | None = None,
    include_done: bool = True,
) -> list[dict]:
    """Return all items linked to a goal (Tasks, Projects, Habits).

    Requires CouchDB direct access. Filters via ``g_in_<goal_id>=true``
    on the item side, optionally narrowed to a specific section.
    """
    _verify_goal(api_client, goal_id)
    if not getattr(api_client, "has_couchdb", False):
        raise ValueError(
            "CouchDB credentials required to list goal-linked items. "
            "Set AMAZING_MARVIN_DB_URI/_DB_NAME/_DB_USER/_DB_PASSWORD."
        )

    selector: dict[str, Any] = {f"g_in_{goal_id}": True}
    if section_id is not None:
        selector[f"g_sec_{goal_id}"] = section_id

    items = api_client.find_docs(selector, limit=500)
    if not include_done:
        items = [i for i in items if not i.get("done")]

    rank_key = f"g_rank_{goal_id}"
    items.sort(key=lambda i: (i.get(rank_key) is None, i.get(rank_key) or 0))
    return items


def link_task_to_goal_impl(
    api_client,
    item_id: str,
    goal_id: str,
    section_id: str | None = None,
    rank: int | None = None,
) -> dict:
    """Attach a Task / Project / Habit to a Goal.

    Sets ``g_in_<goal_id>=true`` and optionally ``g_sec_<goal_id>``,
    ``g_rank_<goal_id>``. Only the **item** doc is mutated — never the
    Goal doc. Bumps ``updatedAt`` on the item.
    """
    goal = _verify_goal(api_client, goal_id)
    _verify_linkable(api_client, item_id)

    if section_id is not None:
        section_ids = {s.get("_id") for s in (goal.get("sections") or [])}
        if section_id not in section_ids:
            raise ValueError(
                f"Section {section_id!r} does not exist on goal {goal_id!r}. "
                f"Existing section ids: {sorted(i for i in section_ids if i)}"
            )

    setters: dict[str, Any] = {f"g_in_{goal_id}": True}
    if section_id is not None:
        setters[f"g_sec_{goal_id}"] = section_id
    if rank is not None:
        setters[f"g_rank_{goal_id}"] = rank
    setters["updatedAt"] = int(time.time() * 1000)

    return api_client.update_document(item_id, setters)


def unlink_task_from_goal_impl(
    api_client,
    item_id: str,
    goal_id: str,
) -> dict:
    """Detach a Task / Project / Habit from a Goal.

    Clears ``g_in_<goal_id>``, ``g_sec_<goal_id>``, ``g_rank_<goal_id>``
    by setting them to ``None`` (Marvin's documented way to remove a
    field via ``/doc/update``). Only the item is mutated.
    """
    _verify_goal(api_client, goal_id)
    _verify_linkable(api_client, item_id)

    setters: dict[str, Any] = {
        f"g_in_{goal_id}": None,
        f"g_sec_{goal_id}": None,
        f"g_rank_{goal_id}": None,
        "updatedAt": int(time.time() * 1000),
    }
    return api_client.update_document(item_id, setters)
