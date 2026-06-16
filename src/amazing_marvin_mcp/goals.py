"""Goals helpers — read, linking, and safe scalar edits.

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

This module exposes:
  - read helpers (get_enriched_goal, get_goal_tasks_impl)
  - task-side linking (link_task_to_goal_impl, unlink_task_from_goal_impl)
  - **scalar Goal-doc edits** (update_goal_impl) — whitelisted set of
    safe fields only. Array fields (sections / challenges / checkIn*)
    and Goal creation/deletion are intentionally NOT exposed here —
    they need their own validation layer (next stage).
"""

from __future__ import annotations

import time
from typing import Any

GOALS_DB = "Goals"
LINKABLE_DBS = {"Tasks", "Categories", "Habits"}

# Status enum — from Marvin wiki and confirmed against live Test Goal.
GOAL_STATUSES = {"backburner", "pending", "active", "done"}

# Whitelisted scalar fields update_goal_impl may write.
# Each entry: (python_type, optional_validator) where validator(value) -> None
# raises ValueError on bad input. None for type means "no type check".
_UPDATE_GOAL_FIELDS: dict[str, tuple[Any, Any]] = {
    # Core
    "title":             (str,  None),
    "note":              (str,  None),
    "status":            (str,  lambda v: _check_in(v, GOAL_STATUSES, "status")),
    "dueDate":           (str,  None),   # "YYYY-MM-DD" or None to clear
    "hasEnd":            (bool, None),
    "hideInDayView":     (bool, None),
    "isStarred":         (int,  lambda v: _check_range(v, 0, 1, "isStarred")),
    "parentId":          (str,  None),
    # Commitment Contract (UI step 6)
    "importance":        (int,  lambda v: _check_range(v, 1, 5, "importance")),
    "difficulty":        (int,  lambda v: _check_range(v, 1, 5, "difficulty")),
    "motivations":       (str,  None),
    # Expectations (UI step 4) — wiki: minutes/week for duration, ints for tasks.
    "expectedTasks":     (int,  lambda v: _check_nonneg(v, "expectedTasks")),
    "expectedDuration":  (int,  lambda v: _check_nonneg(v, "expectedDuration")),
    "expectedHabits":    (str,  None),   # free-text grade like "B-"
}

# Fields whose presence indicates the UI checklist step is satisfied.
_HAS_EXPECTATIONS_FIELDS = ("expectedTasks", "expectedDuration", "expectedHabits")


def _check_in(value, allowed, field_name):
    if value not in allowed:
        raise ValueError(
            f"{field_name}={value!r} not in allowed values {sorted(allowed)}"
        )


def _check_range(value, lo, hi, field_name):
    if not (lo <= value <= hi):
        raise ValueError(f"{field_name}={value!r} not in range [{lo}, {hi}]")


def _check_nonneg(value, field_name):
    if value < 0:
        raise ValueError(f"{field_name}={value!r} must be >= 0")


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

    enriched["setup_status"] = _compute_setup_status(goal, has_actions=bool(linked))
    return enriched


def _compute_setup_status(goal: dict, *, has_actions: bool) -> dict:
    """Map the Marvin UI's 6-step goal setup checklist to booleans.

    UI steps (Marvin in-app help text):
      1. Check Goal              — title set & positively framed (we only check non-empty)
      2. Trackers & Progress     — any ``trackerProgress_*`` key OR has_trackers metadata
      3. Add Actions             — at least one Task/Project/Habit linked
      4. Set Expectations        — expectedTasks / Duration / Habits set
      5. Set Up Check-Ins        — checkIn enabled
      6. Commit                  — status != "pending" (user filled commitment contract)

    Returns ``{is_*: bool, missing_steps: [str]}`` so callers can show a
    "next steps" hint to the user.
    """
    title = (goal.get("title") or "").strip()
    has_title = bool(title)
    has_trackers = any(k.startswith("trackerProgress_") for k in goal.keys())
    has_expectations = any(
        goal.get(f) not in (None, 0, "", []) for f in _HAS_EXPECTATIONS_FIELDS
    )
    has_checkins = bool(goal.get("checkIn"))
    is_committed = goal.get("status") not in (None, "pending")

    steps = {
        "has_title": has_title,
        "has_trackers": has_trackers,
        "has_actions": has_actions,
        "has_expectations": has_expectations,
        "has_checkins": has_checkins,
        "is_committed": is_committed,
    }
    labels = {
        "has_title": "1. Check Goal — set a title",
        "has_trackers": "2. Set up Trackers & Progress",
        "has_actions": "3. Add Actions (Tasks/Projects/Habits)",
        "has_expectations": "4. Set Expectations (expectedTasks/Duration/Habits)",
        "has_checkins": "5. Set Up Check-Ins",
        "is_committed": "6. Commit (status != 'pending')",
    }
    missing = [labels[k] for k, v in steps.items() if not v]
    return {**steps, "missing_steps": missing}


def update_goal_impl(api_client, goal_id: str, **changes) -> dict:
    """Patch whitelisted scalar fields on a Goal document.

    Only scalar fields are accepted (see ``_UPDATE_GOAL_FIELDS``). Array
    fields (sections, challenges, checkIn questions, labelIds) and
    tracker config are intentionally excluded — they need their own
    typed CRUD operations.

    Pre-flight verifies the doc actually has ``db == "Goals"``. Bumps
    ``updatedAt``. Returns the API response from ``/doc/update``.

    Pass ``None`` for a value to clear that field (Marvin's documented
    semantics for /doc/update).
    """
    if not changes:
        raise ValueError("update_goal requires at least one field to change.")

    unknown = set(changes) - set(_UPDATE_GOAL_FIELDS)
    if unknown:
        raise ValueError(
            f"Field(s) {sorted(unknown)} not in update_goal whitelist. "
            f"Allowed: {sorted(_UPDATE_GOAL_FIELDS)}. For array fields "
            f"(sections, challenges, checkIn*, labelIds) use a future "
            f"typed CRUD tool — not exposed yet."
        )

    for field, value in changes.items():
        if value is None:
            continue  # clear semantics — skip type/validator checks
        expected_type, validator = _UPDATE_GOAL_FIELDS[field]
        if expected_type is bool and not isinstance(value, bool):
            raise ValueError(
                f"{field}={value!r} must be bool (got {type(value).__name__})"
            )
        if expected_type is int and (isinstance(value, bool) or not isinstance(value, int)):
            raise ValueError(
                f"{field}={value!r} must be int (got {type(value).__name__})"
            )
        if expected_type is str and not isinstance(value, str):
            raise ValueError(
                f"{field}={value!r} must be str (got {type(value).__name__})"
            )
        if validator is not None:
            validator(value)

    _verify_goal(api_client, goal_id)

    setters = dict(changes)
    setters["updatedAt"] = int(time.time() * 1000)
    return api_client.update_document(goal_id, setters)


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
