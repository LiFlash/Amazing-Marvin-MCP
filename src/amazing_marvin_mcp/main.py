import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import BeforeValidator

from .analytics import (
    get_completed_tasks as get_completed_tasks_impl,
)
from .analytics import (
    get_daily_productivity_overview as get_daily_productivity_overview_impl,
)
from .analytics import (
    get_productivity_summary_for_time_range as get_productivity_summary_for_time_range_impl,
)
from .api import create_api_client
from .date_utils import DateUtils
from .enrichment import enrich_via_couchdb
from .habits import (
    get_enriched_habit as get_enriched_habit_impl,
    get_enriched_habits as get_enriched_habits_impl,
    get_habit_streak_impl,
)
from .models import TaskUpdateRequest
from .setters_builder import build_setters
from .projects import (
    create_project_with_tasks as create_project_impl,
)
from .projects import (
    get_project_overview as get_project_overview_impl,
)
from .response_models import StandardResponse
from .smartlists import (
    WRITABLE_CLAUSE_FIELDS,
    create_smart_list_impl,
    delete_smart_list_impl,
    get_smart_list_impl,
    list_smart_lists_impl,
    update_smart_list_impl,
)
from .tasks import (
    batch_create_tasks as batch_create_tasks_impl,
)
from .tasks import (
    get_all_tasks_impl,
    get_child_tasks_recursive,
)
from .tool_converter import (
    create_error_response,
    create_simple_response,
    create_task_response,
)

def _coerce_json_list(v: Any) -> list:
    """Accept a JSON string that encodes a list, or pass a real list through.

    Some MCP transports (e.g. Claude Code stdio) serialise complex parameters as
    JSON strings rather than native types. This validator normalises both forms.
    """
    if isinstance(v, str):
        v = v.strip()
        # Bare comma-separated values without brackets
        if not v.startswith("["):
            v = f"[{v}]"
        return json.loads(v)
    return v


def _coerce_json_dict(v: Any) -> dict:
    """Accept a JSON string that encodes a dict, or pass a real dict through."""
    if isinstance(v, str):
        v = v.strip()
        # Attempt to handle single-quoted or unquoted keys via re normalisation
        v = re.sub(r"'", '"', v)
        return json.loads(v)
    return v


# Type aliases with automatic coercion for MCP tool parameters
JsonStrList = Annotated[list[str], BeforeValidator(_coerce_json_list)]
JsonDictList = Annotated[list[dict], BeforeValidator(_coerce_json_list)]
JsonDict = Annotated[dict, BeforeValidator(_coerce_json_dict)]


# Initialize logger
logger = logging.getLogger(__name__)

# Initialize MCP
mcp: FastMCP = FastMCP(name="amazing-marvin-mcp")


@mcp.tool()
async def get_tasks(debug: bool = False) -> StandardResponse:
    """Get tasks scheduled for today only (no overdue, no completed).

    Use when: user asks "what's on my plate today", "today's tasks", or you only need items the
    user explicitly scheduled for today (Marvin's `day` field == today).
    Don't use for: overdue tasks (use get_due_items), completed tasks (use get_completed_tasks),
    full daily picture (use get_daily_productivity_overview), or searches across projects
    (use get_all_tasks).

    Returns: `data.tasks` (list of today's task docs) plus standard envelope.
    Hits /todayItems.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        today = DateUtils.get_today()
        raw_tasks = api_client.get_tasks(date=today)

        return create_task_response(
            api_client=api_client,
            raw_tasks=raw_tasks,
            summary_text=f"Retrieved {len(raw_tasks)} scheduled tasks for today",
            api_endpoint="/todayItems",
            api_calls_made=4,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get tasks")
        return create_error_response(e, "/todayItems", debug, start_time)


@mcp.tool()
async def get_projects(debug: bool = False) -> StandardResponse:
    """List all projects (category docs with type=="project").

    When CouchDB direct access is configured, the returned docs are
    enriched with the fields the /api/categories projection strips —
    `createdAt`, `updatedAt`, `db`, `rank`, `workedOnAt`. Without
    CouchDB, the REST projection is returned as-is (cheap path).

    Use when: user needs to pick a project, browse the project tree, or you need a project's _id.
    Don't use for: tasks inside a project (use get_project_overview or get_child_tasks).

    Returns: `data` = list of project docs (each with _id, title, parentId, type, ...).
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        projects = api_client.get_projects()
        projects = enrich_via_couchdb(api_client, projects, "Categories")

        return create_simple_response(
            data=projects,
            summary_text=f"Retrieved {len(projects)} projects",
            api_endpoint="/categories",
            api_calls_made=2 if getattr(api_client, "has_couchdb", False) else 1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get projects")
        return create_error_response(e, "/categories", debug, start_time)


@mcp.tool()
async def get_categories(debug: bool = False) -> StandardResponse:
    """List all categories (both type=="category" and type=="project").

    When CouchDB direct access is configured, the returned docs are
    enriched with `createdAt`, `updatedAt`, `db`, `done`, `doneDate`,
    `rank`, `isFrogged` — fields the /api/categories projection strips.

    Use when: you need the full container tree including both categories and projects.
    Don't use for: only projects (use get_projects) or a single container's children
    (use get_child_tasks).

    Returns: `data` = list of category/project docs.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        categories = api_client.get_categories()
        categories = enrich_via_couchdb(api_client, categories, "Categories")

        return create_simple_response(
            data=categories,
            summary_text=f"Retrieved {len(categories)} categories",
            api_endpoint="/categories",
            api_calls_made=2 if getattr(api_client, "has_couchdb", False) else 1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get categories")
        return create_error_response(e, "/categories", debug, start_time)


@mcp.tool()
async def get_due_items(debug: bool = False) -> StandardResponse:
    """Get tasks whose `dueDate` is today or in the past and that are still open.

    Use when: user asks "what's overdue", "what's due", or you need to triage urgent work.
    Don't use for: items merely scheduled for today via the `day` field (use get_tasks) or
    completed-but-was-due items (use get_completed_tasks). For a full daily view including
    overdue + today + completed, use get_daily_productivity_overview.

    Returns: `data.due_items` (list). Hits /dueItems.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        due_items = api_client.get_due_items()

        return create_simple_response(
            data={"due_items": due_items},
            summary_text=f"Retrieved {len(due_items)} overdue/due items",
            api_endpoint="/dueItems",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get due items")
        return create_error_response(e, "/dueItems", debug, start_time)


@mcp.tool()
async def get_child_tasks(
    parent_id: str, recursive: bool = False, debug: bool = False
) -> StandardResponse:
    """Get the direct (or recursive) children of a task / project / category.

    Use when: you have a parent _id and need its sub-items split into tasks vs. projects
    vs. categories. For a full project dashboard with progress metrics, prefer
    get_project_overview.

    Args:
        parent_id: Opaque CouchDB _id of the parent (task, project, or category).
        recursive: If True, walks the entire descendant tree (one /children call per
            sub-container — can be expensive on large trees). Default False = one call.

    Returns: `data` with keys `tasks`, `projects`, `categories`, `all_children`,
    `total_children`, plus per-bucket counts. Hits /children.

    Note: Marvin's /children endpoint is experimental; some non-container parents may
    return empty.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        if recursive:
            result = get_child_tasks_recursive(api_client, parent_id)
            api_calls = result.get("api_calls_made", 3)  # Estimate for recursive calls
        else:
            children = api_client.get_children(parent_id)
            # Categorize non-recursive results for consistency
            tasks = [
                {**item, "type": "task"} if "type" not in item else item
                for item in children
                if item.get("type") not in ("project", "category")
            ]
            projects = [item for item in children if item.get("type") == "project"]
            categories = [item for item in children if item.get("type") == "category"]

            result = {
                "parent_id": parent_id,
                "total_children": len(children),
                "tasks": tasks,
                "projects": projects,
                "categories": categories,
                "task_count": len(tasks),
                "project_count": len(projects),
                "category_count": len(categories),
                "all_children": children,
                "recursive": False,
            }
            api_calls = 1

        return create_simple_response(
            data=result,
            summary_text=f"Retrieved {result.get('total_children', 0)} child items for parent {parent_id}",
            api_endpoint="/children",
            api_calls_made=api_calls,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get child tasks for %s", parent_id)
        return create_error_response(e, "/children", debug, start_time)


@mcp.tool()
async def get_all_tasks(
    label: str | None = None,
    fields: list[str] | None = None,
    debug: bool = False,
) -> StandardResponse:
    """Recursively walk the full project tree and return every task (heavy operation).

    Use when: user asks for "all tasks", "tasks with label X across everything", or you need
    to search/aggregate across the whole system.
    Don't use for: today's work (use get_tasks / get_daily_productivity_overview), tasks
    inside one project (use get_project_overview or get_child_tasks), or completed tasks
    (use get_completed_tasks). This makes many API calls — avoid in hot paths.

    Args:
        label: Optional label name (not _id) to filter by. None = return all tasks.
        fields: Optional whitelist of field names per task dict (e.g. ["_id","title","day"]).
            Unknown fields are silently ignored. None = return every field.

    Returns: `data` with `tasks`, `total_tasks`, plus per-project grouping.
    Hits /categories + /children (many calls).
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = get_all_tasks_impl(api_client, label, fields)

        # Estimate API calls based on typical project count
        estimated_api_calls = result.get("api_calls_made", 5)

        return create_simple_response(
            data=result,
            summary_text=f"Retrieved {result.get('total_tasks', 0)} tasks across all projects"
            + (f" with label '{label}'" if label else ""),
            api_endpoint="/categories + /children",
            api_calls_made=estimated_api_calls,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get all tasks")
        return create_error_response(e, "/categories + /children", debug, start_time)


@mcp.tool()
async def get_document(item_id: str, debug: bool = False) -> StandardResponse:
    """Read the raw CouchDB document for ANY Marvin item by _id (generic, low-level).

    Requires AMAZING_MARVIN_FULL_ACCESS_TOKEN.

    Use when: no specific tool exists for the doc type you need (e.g. Goals, Labels,
    raw Habit history, time-block details) AND you need fields the REST projection hides
    (such as `db`, `history`, `note`, full timestamps).
    Don't use for:
      - SmartLists  -> use get_smart_list (validates db field)
      - Habits with streak questions -> use get_habit_streak
      - Tasks if you only need standard fields -> use get_tasks / get_all_tasks instead
      - Updating fields -> use update_task / update_document / update_smart_list

    Args:
        item_id: Opaque CouchDB _id of the document.

    Returns: `data` = the raw doc dict (includes `_id`, `_rev`, `db`, and all native fields).
    Hits /doc?id=<id>.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = api_client.get_document(item_id)

        return create_simple_response(
            data=result,
            summary_text=f"Retrieved document {item_id}",
            api_endpoint=f"/doc?id={item_id}",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get document %s", item_id)
        return create_error_response(e, "/doc", debug, start_time)


@mcp.tool()
async def update_document(
    item_id: str, setters: dict[str, Any], debug: bool = False
) -> StandardResponse:
    """Patch arbitrary fields on any Marvin document via raw setter dict (generic, unsafe).

    Requires AMAZING_MARVIN_FULL_ACCESS_TOKEN. No schema validation — typos in field names
    silently no-op or corrupt the document.

    Use when: a more specific tool does not cover what you need (e.g. clearing a field by
    setting it to null, editing an exotic doc type, editing a label/goal).
    Don't use for:
      - SmartLists  -> use update_smart_list (validates db + whitelist)
      - Habits (record/undo) -> use record_habit / undo_habit
      - Standard task fields -> use update_task (typed, named, validated)
      - Marking a task complete -> use mark_task_done

    Args:
        item_id: Opaque CouchDB _id of the document to update.
        setters: Field -> new value dict, e.g. {"note": "new text", "dueDate": "2025-06-10",
            "title": "New title"}. Use `null` to clear a field.

    Returns: `data` = updated doc (includes new `_rev`). Hits /doc/update.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = api_client.update_document(item_id, setters)

        return create_simple_response(
            data=result,
            summary_text=f"Updated document {item_id} with {len(setters)} field(s)",
            api_endpoint="/doc/update",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to update document %s", item_id)
        return create_error_response(e, "/doc/update", debug, start_time)


@mcp.tool()
async def delete_document(item_id: str, debug: bool = False) -> StandardResponse:
    """Permanently delete a task, or a project/category that has NO children.

    Requires AMAZING_MARVIN_FULL_ACCESS_TOKEN. NOT REVERSIBLE.

    Safety pre-flight:
      - db=="Tasks" with non-container type  -> deleted immediately.
      - type in ("project","category")  -> deleted only if no children exist.
      - Any other doc type (Goals, Labels, Habits, SmartLists, ...)  -> blocked with error.

    Use when: deleting a task or an empty project/category container.
    Don't use for:
      - SmartLists  -> use delete_smart_list (validates db field)
      - Containers that still have children -> move/delete the children first
      - "Marking done" instead of removing  -> use mark_task_done

    Args:
        item_id: Opaque CouchDB _id of the document.

    Returns: `data` with `deleted_title`, `deleted_type`. Hits /doc?id (safety read) +
    /doc/delete (+ optional /children for containers).
    """
    start_time = time.time()
    try:
        api_client = create_api_client()

        # Safety pre-flight: read the document before deleting
        doc = api_client.get_document(item_id)
        db = doc.get("db", "")
        doc_type = doc.get("type", "")
        title = doc.get("title", item_id)
        api_calls = 1

        is_task = db == "Tasks" and doc_type not in ("project", "category")
        is_container = doc_type in ("project", "category")

        if is_task:
            pass  # safe to delete
        elif is_container:
            children = api_client.get_children(item_id)
            api_calls += 1
            if children:
                return create_error_response(
                    ValueError(
                        f"'{title}' is a {doc_type} with {len(children)} children. "
                        "Delete or move all children before deleting the container."
                    ),
                    "/doc/delete",
                    debug,
                    start_time,
                )
        else:
            return create_error_response(
                ValueError(
                    f"'{title}' has db='{db}', type='{doc_type}'. "
                    "Only tasks and empty projects/categories may be deleted."
                ),
                "/doc/delete",
                debug,
                start_time,
            )

        result = api_client.delete_document(item_id)
        api_calls += 1

        return create_simple_response(
            data={**result, "deleted_title": title, "deleted_type": doc_type or "task"},
            summary_text=f"Deleted {doc_type or 'task'} '{title}' ({item_id})",
            api_endpoint="/doc/delete",
            api_calls_made=api_calls,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to delete document %s", item_id)
        return create_error_response(e, "/doc/delete", debug, start_time)


@mcp.tool()
async def update_task(
    item_id: str,
    title: str | None = None,
    due_date: str | None = None,
    scheduled_date: str | None = None,
    note: str | None = None,
    label_ids: list[str] | None = None,
    priority: str | None = None,
    parent_id: str | None = None,
    is_starred: bool | None = None,
    is_frogged: bool | None = None,
    time_estimate: int | None = None,
    backburner: bool | None = None,
    debug: bool = False,
) -> StandardResponse:
    """Update a task's fields via friendly typed parameters (preferred over update_document).

    Requires AMAZING_MARVIN_FULL_ACCESS_TOKEN. Pass only the params you want to change —
    `None` means "leave unchanged" (it does NOT clear the field). To CLEAR a field, fall
    back to update_document with explicit null/empty setters.

    Use when: editing any task field — title, due/scheduled, notes, labels, priority,
    parent, flags, time estimate.
    Don't use for: marking a task complete (use mark_task_done), creating new tasks
    (use create_task / batch_create_tasks), or updating non-Task docs (use update_document
    for arbitrary docs, update_smart_list for SmartLists).

    Args:
        item_id: Opaque CouchDB _id of the task.
        title: New title.
        due_date: Due date as "YYYY-MM-DD" (maps to Marvin `dueDate`).
        scheduled_date: Date the task is planned for, as "YYYY-MM-DD" (maps to `day`).
        note: Free-text notes / description (maps to `note`).
        label_ids: List of label _ids (e.g. ["lbl_abc","lbl_def"]; maps to `labelIds`).
        priority: Priority string accepted by Marvin (e.g. "high").
        parent_id: New parent project/category _id (maps to `parentId`).
        is_starred: Bool, whether the task is starred.
        is_frogged: Bool, whether the task is "frogged" (eat-the-frog flag).
        time_estimate: Estimate in minutes (int).
        backburner: Bool, whether to push to the backburner list.

    Returns: `data` = updated doc with new `_rev`. Hits /doc/update.

    Note: this tool does not currently expose `plannedWeek` ("YYYY-Www") or `plannedMonth`
    ("YYYY-MM") — use update_document for those.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        update_req = TaskUpdateRequest(
            item_id=item_id,
            title=title,
            due_date=due_date,
            scheduled_date=scheduled_date,
            note=note,
            label_ids=label_ids,
            priority=priority,
            parent_id=parent_id,
            is_starred=is_starred,
            is_frogged=is_frogged,
            time_estimate=time_estimate,
            backburner=backburner,
        )
        setters = build_setters(update_req)
        result = api_client.update_document(item_id, setters)

        return create_simple_response(
            data=result,
            summary_text=f"Updated task {item_id}",
            api_endpoint="/doc/update",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to update task %s", item_id)
        return create_error_response(e, "/doc/update", debug, start_time)


@mcp.tool()
async def get_labels(debug: bool = False) -> StandardResponse:
    """List all labels (tags) defined in the account.

    Use when: you need a label's _id to filter tasks or to set `labelIds` via update_task.
    Don't use for: tasks carrying a label (use get_all_tasks(label="X")).

    Returns: `data.labels` (list of label docs with _id + title). Hits /labels.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        labels = api_client.get_labels()

        return create_simple_response(
            data={"labels": labels},
            summary_text=f"Retrieved {len(labels)} labels",
            api_endpoint="/labels",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get labels")
        return create_error_response(e, "/labels", debug, start_time)


@mcp.tool()
async def get_goals(debug: bool = False) -> StandardResponse:
    """List all goals defined in the account.

    Use when: you need a goal's _id (e.g. to set `goalId` clauses on a SmartList) or to
    inspect the user's strategic goals.
    Don't use for: tasks linked to a goal (filter via SmartList with `goalId` clause).

    Returns: `data.goals` (list). Hits /goals.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        goals = api_client.get_goals()

        return create_simple_response(
            data={"goals": goals},
            summary_text=f"Retrieved {len(goals)} goals",
            api_endpoint="/goals",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get goals")
        return create_error_response(e, "/goals", debug, start_time)


@mcp.tool()
async def get_account_info(debug: bool = False) -> StandardResponse:
    """Return the authenticated user's account info (email, settings, profile metadata).

    Use when: verifying which account is connected, or fetching user-level settings.
    Don't use for: testing connectivity (use test_api_connection).

    Returns: `data.account` (raw `/me` payload). Hits /me.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        account = api_client.get_account_info()

        return create_simple_response(
            data={"account": account},
            summary_text="Retrieved account information",
            api_endpoint="/me",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get account info")
        return create_error_response(e, "/me", debug, start_time)


@mcp.tool()
async def get_currently_tracked_item(debug: bool = False) -> StandardResponse:
    """Return the single task currently being time-tracked (or "not tracking" message).

    Marvin allows only ONE actively tracked item at a time (per account). When nothing is
    tracked, the API returns a `{"message": ...}` payload — this tool's summary_text reflects
    that.

    Use when: checking whether a timer is running, or fetching the active task's _id/title.
    Don't use for: history of past tracks (use get_time_tracks) or starting/stopping
    (use start_time_tracking / stop_time_tracking).

    Returns: `data.tracked_item` (raw payload). Hits /me/currentlyTrackedItem.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        tracked_item = api_client.get_currently_tracked_item()

        is_tracking = tracked_item and "message" not in tracked_item

        return create_simple_response(
            data={"tracked_item": tracked_item},
            summary_text="Currently tracking a task"
            if is_tracking
            else "No task currently being tracked",
            api_endpoint="/me/currentlyTrackedItem",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get currently tracked item")
        return create_error_response(e, "/me/currentlyTrackedItem", debug, start_time)


@mcp.tool()
async def create_task(
    title: str,
    project_id: str | None = None,
    category_id: str | None = None,
    due_date: str | None = None,
    note: str | None = None,
    debug: bool = False,
) -> StandardResponse:
    """Create a single new task.

    Use when: adding one task to the system.
    Don't use for: many tasks at once (use batch_create_tasks), or creating a project
    along with its initial tasks (use create_project_with_tasks).

    Args:
        title: Task title (required, non-empty).
        project_id: Optional parent project _id (maps to `parentId`).
        category_id: Optional category _id for organization (maps to `categoryId`).
        due_date: Optional due date as "YYYY-MM-DD".
        note: Optional free-text description (maps to `note`).

    Returns: `data.created_task` (the new task doc). Hits /addTask.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()

        task_data = {"title": title}
        if project_id:
            task_data["parentId"] = project_id
        if category_id:
            task_data["categoryId"] = category_id
        if due_date:
            task_data["dueDate"] = due_date
        if note:
            task_data["note"] = note

        created_task = api_client.create_task(task_data)

        return create_simple_response(
            data={"created_task": created_task},
            summary_text=f"Created task: {title}",
            api_endpoint="/addTask",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to create task '%s'", title)
        return create_error_response(e, "/addTask", debug, start_time)


@mcp.tool()
async def mark_task_done(
    item_id: str, timezone_offset: int = 0, debug: bool = False
) -> StandardResponse:
    """Mark a single task complete (records completion date + handles recurring tasks).

    Use when: closing out one task.
    Don't use for: multiple tasks at once (use batch_mark_done) or general field edits
    (use update_task).

    Warning — NOT idempotent for RECURRING tasks: each call advances the recurrence to
    the next due date / generates a new occurrence. Calling twice in a row on a daily
    recurring task will tick off TWO occurrences. For one-shot tasks it is effectively
    idempotent (the task stays done).

    Args:
        item_id: Opaque CouchDB _id of the task.
        timezone_offset: Minutes offset from UTC for completion timestamp (e.g. -480 = PST,
            60 = CET). Default 0 = UTC.

    Returns: `data.completed_task` = the updated/created completion doc. Hits /markDone.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        completed_task = api_client.mark_task_done(item_id, timezone_offset)

        return create_simple_response(
            data={"completed_task": completed_task},
            summary_text=f"Marked task {item_id} as completed",
            api_endpoint="/markDone",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to mark task %s as done", item_id)
        return create_error_response(e, "/markDone", debug, start_time)


@mcp.tool()
async def test_api_connection(debug: bool = False) -> StandardResponse:  # noqa: PT028
    """Ping the Marvin API to verify credentials and reachability.

    Use when: troubleshooting auth/setup, first call after configuring tokens, or as a
    cheap health check.
    Don't use for: actual account info (use get_account_info — same /me endpoint, but
    returns the payload).

    Returns: `data.status` (typically "OK" on success). Hits /me.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        status = api_client.test_api_connection()

        return create_simple_response(
            data={"status": status},
            summary_text=f"API connection test: {status}",
            api_endpoint="/me",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to test API connection")
        return create_error_response(e, "/me", debug, start_time)


@mcp.tool()
async def start_time_tracking(task_id: str, debug: bool = False) -> StandardResponse:
    """Start the timer on a task. ONLY ONE TASK CAN BE TRACKED AT A TIME (account-wide).

    Starting a new track while another task is being tracked silently stops the previous
    one on Marvin's side. Always check with get_currently_tracked_item first if you want
    to avoid clobbering an existing timer.

    Use when: user wants to begin focused work and measure time on a task.
    Don't use for: stopping (use stop_time_tracking) or just reading current state
    (use get_currently_tracked_item).

    Args:
        task_id: Opaque CouchDB _id of the task to track.

    Returns: `data.tracking` (raw payload). Hits /startTimeTracking.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        tracking = api_client.start_time_tracking(task_id)

        return create_simple_response(
            data={"tracking": tracking},
            summary_text=f"Started time tracking for task {task_id}",
            api_endpoint="/startTimeTracking",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to start time tracking for task %s", task_id)
        return create_error_response(e, "/startTimeTracking", debug, start_time)


@mcp.tool()
async def stop_time_tracking(task_id: str, debug: bool = False) -> StandardResponse:
    """Stop the timer on a task (records the elapsed duration to its time-track history).

    Use when: user wants to pause/end focused work on the currently tracked task.
    Don't use for: starting a track (use start_time_tracking) or reading status
    (use get_currently_tracked_item).

    Args:
        task_id: Opaque CouchDB _id of the task that is currently being tracked. Should
            match the task returned by get_currently_tracked_item — calling stop on a
            non-tracked task is a no-op on Marvin's side.

    Returns: `data.tracking` (raw payload). Hits /stopTimeTracking.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        tracking = api_client.stop_time_tracking(task_id)

        return create_simple_response(
            data={"tracking": tracking},
            summary_text=f"Stopped time tracking for task {task_id}",
            api_endpoint="/stopTimeTracking",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to stop time tracking for task %s", task_id)
        return create_error_response(e, "/stopTimeTracking", debug, start_time)


@mcp.tool()
async def get_time_tracks(task_ids: list[str], debug: bool = False) -> StandardResponse:
    """Fetch recorded time-track entries (start/stop history) for the given tasks.

    Use when: you need actual time spent per task — durations, individual tracking
    sessions, totals.
    Don't use for: the currently-running timer (use get_currently_tracked_item) or a high
    level summary (use time_tracking_summary).

    Args:
        task_ids: List of task _ids whose time-track history you want, e.g.
            ["task_abc","task_def"].

    Returns: `data.time_tracks` (raw per-task tracking entries). Hits /timeTracks.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        time_tracks = api_client.get_time_tracks(task_ids)

        return create_simple_response(
            data={"time_tracks": time_tracks},
            summary_text=f"Retrieved time tracking data for {len(task_ids)} tasks",
            api_endpoint="/timeTracks",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get time tracks")
        return create_error_response(e, "/timeTracks", debug, start_time)


@mcp.tool()
async def claim_reward_points(
    points: int, item_id: str, date: str, debug: bool = False
) -> StandardResponse:
    """Award the user reward points for completing a task (Marvin gamification).

    Workflow context: in Marvin, completing a task can earn the user "reward points" they
    can later spend on real-world rewards. claim_reward_points ADDS points to the balance,
    spend_reward_points DEDUCTS them, and unclaim_reward_points reverses an earlier claim.
    Use get_kudos_info to read the current balance.

    Side effect: increases the user's point balance.

    Args:
        points: Number of points to add (positive int).
        item_id: Opaque CouchDB _id of the task the points are credited for.
        date: Date the claim is recorded for, as "YYYY-MM-DD".

    Returns: `data.reward` (raw payload). Hits /claimRewardPoints.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        reward = api_client.claim_reward_points(points, item_id, date)

        return create_simple_response(
            data={"reward": reward},
            summary_text=f"Claimed {points} reward points for task {item_id}",
            api_endpoint="/rewardPoints",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to claim reward points")
        return create_error_response(e, "/rewardPoints", debug, start_time)


@mcp.tool()
async def get_kudos_info(debug: bool = False) -> StandardResponse:
    """Get current reward-point balance and achievement / kudos info.

    Use when: user asks "how many points do I have", "what rewards am I close to",
    or you need the balance before calling spend_reward_points.
    Don't use for: actually changing the balance (use claim_reward_points /
    spend_reward_points / unclaim_reward_points).

    Returns: `data.kudos` (raw payload — balance, achievements, etc.). Hits /me/kudos.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        kudos = api_client.get_kudos_info()

        return create_simple_response(
            data={"kudos": kudos},
            summary_text="Retrieved kudos and achievement information",
            api_endpoint="/me/kudos",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get kudos info")
        return create_error_response(e, "/me/kudos", debug, start_time)


@mcp.tool()
async def create_project(
    title: str, project_type: str = "project", debug: bool = False
) -> StandardResponse:
    """Create a new project (or category) container.

    Use when: adding an empty new project / category.
    Don't use for: project + initial tasks in one shot (use create_project_with_tasks).

    Args:
        title: Container name (required).
        project_type: "project" (default) or "category".

    Returns: `data.created_project` (raw doc). Hits /addCategory.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()

        project_data = {"title": title, "type": project_type}
        created_project = api_client.create_project(project_data)

        return create_simple_response(
            data={"created_project": created_project},
            summary_text=f"Created project: {title}",
            api_endpoint="/addCategory",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to create project '%s'", title)
        return create_error_response(e, "/addCategory", debug, start_time)


@mcp.tool()
async def create_project_with_tasks(
    project_title: str,
    task_titles: list[str],
    project_type: str = "project",
    debug: bool = False,
) -> StandardResponse:
    """Create a project AND seed it with multiple tasks in one workflow.

    Use when: scaffolding a new project from a known task list.
    Don't use for: an empty project (use create_project) or adding tasks to an existing
    project (use batch_create_tasks with project_id set).

    Args:
        project_title: Display name for the new project.
        task_titles: List of task titles, one per task to create.
        project_type: "project" (default) or "category" for the container.

    Returns: `data` with the created project plus list of created tasks. Hits /addCategory
    + /addTask (one per title).
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = create_project_impl(
            api_client, project_title, task_titles, project_type
        )

        # Estimate API calls: 1 for project + 1 per task
        api_calls = 1 + len(task_titles)

        return create_simple_response(
            data=result,
            summary_text=f"Created project '{project_title}' with {len(task_titles)} tasks",
            api_endpoint="/addCategory + /addTask",
            api_calls_made=api_calls,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to create project with tasks")
        return create_error_response(e, "/addCategory + /addTask", debug, start_time)


@mcp.tool()
async def get_project_overview(
    project_id: str, debug: bool = False
) -> StandardResponse:
    """Get a project's full dashboard: tasks, sub-projects, and progress metrics.

    Use when: user opens / drills into a specific project (you have its _id) and wants
    counts, completion ratios, and contained items.
    Don't use for: just the raw children list (use get_child_tasks) or just the project
    metadata (use get_document).

    Args:
        project_id: Opaque CouchDB _id of the project.

    Returns: `data` with project doc + grouped children + progress stats.
    Hits /categories + /children.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = get_project_overview_impl(api_client, project_id)

        return create_simple_response(
            data=result,
            summary_text=f"Retrieved overview for project {project_id}",
            api_endpoint="/categories + /children",
            api_calls_made=2,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get project overview for %s", project_id)
        return create_error_response(e, "/categories + /children", debug, start_time)


@mcp.tool()
async def get_daily_productivity_overview(debug: bool = False) -> StandardResponse:
    """One-stop daily dashboard: today's scheduled + overdue + completed + planning hints.

    Primary tool for the "what's my day look like" request — consolidates multiple endpoints
    in one response so the LLM does not need to call several tools.

    Use when: user asks for a full daily picture, end-of-day review, or "give me an overview".
    Don't use for: only today (use get_tasks), only overdue (use get_due_items), only
    completed (use get_completed_tasks), or system-wide search (use get_all_tasks).

    Returns: `data` with `today`, `overdue`, `completed`, planning insights, counts.
    Hits /todayItems + /dueItems + /doneItems.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = get_daily_productivity_overview_impl(api_client)

        return create_simple_response(
            data=result,
            summary_text="Retrieved comprehensive daily productivity overview",
            api_endpoint="/todayItems + /dueItems + /doneItems",
            api_calls_made=3,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get daily productivity overview")
        return create_error_response(
            e, "/todayItems + /dueItems + /doneItems", debug, start_time
        )


@mcp.tool()
async def batch_create_tasks(
    task_list: list[str],
    project_id: str | None = None,
    category_id: str | None = None,
    debug: bool = False,
) -> StandardResponse:
    """Create many tasks in one tool call (one /addTask request per title).

    Use when: bulk-adding tasks (e.g. inbox dump, meeting action items) to one place.
    Don't use for: a single task (use create_task) or project + tasks scaffold
    (use create_project_with_tasks).

    Args:
        task_list: List of task titles (one task per entry).
        project_id: Optional parent project _id assigned to every created task.
        category_id: Optional category _id assigned to every created task.

    Returns: `data` with `created_tasks`, `success_count`, `failure_count`.
    Hits /addTask N times.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = batch_create_tasks_impl(api_client, task_list, project_id, category_id)

        return create_simple_response(
            data=result,
            summary_text=f"Created {result.get('success_count', 0)} tasks in batch",
            api_endpoint="/addTask",
            api_calls_made=len(task_list),
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to batch create tasks")
        return create_error_response(e, "/addTask", debug, start_time)


@mcp.tool()
async def batch_mark_done(task_ids: list[str], debug: bool = False) -> StandardResponse:
    """Mark several tasks complete in one tool call (one /markDone request per task).

    Use when: closing out a list of tasks (e.g. end-of-day cleanup).
    Don't use for: a single task (use mark_task_done) — same semantics, less overhead.

    Warning: same caveat as mark_task_done — for RECURRING tasks each entry advances the
    recurrence. Failures are collected per-task; partial success is possible.

    Args:
        task_ids: List of task _ids to mark done, e.g. ["task_abc","task_def"].

    Returns: `data` with `completed_tasks`, `failed_tasks`, `success_count`,
    `failure_count`, `total_requested`. Hits /markDone N times.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()

        completed_tasks = []
        failed_tasks = []

        for task_id in task_ids:
            try:
                completed_task = api_client.mark_task_done(task_id)
                completed_tasks.append(completed_task)
            except Exception as e:
                failed_tasks.append({"task_id": task_id, "error": str(e)})

        result = {
            "completed_tasks": completed_tasks,
            "failed_tasks": failed_tasks,
            "success_count": len(completed_tasks),
            "failure_count": len(failed_tasks),
            "total_requested": len(task_ids),
        }

        return create_simple_response(
            data=result,
            summary_text=f"Marked {len(completed_tasks)} of {len(task_ids)} tasks as done",
            api_endpoint="/markDone",
            api_calls_made=len(task_ids),
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to batch mark tasks done")
        return create_error_response(e, "/markDone", debug, start_time)


@mcp.tool()
async def time_tracking_summary(debug: bool = False) -> StandardResponse:
    """Aggregate view of time tracking: current timer + account stats + kudos in one call.

    Use when: user asks "am I tracking anything", "what's my tracking status", or wants a
    quick productivity snapshot without three separate tool calls.
    Don't use for: per-task tracking history (use get_time_tracks) or starting/stopping
    a timer (use start_time_tracking / stop_time_tracking) or full daily picture
    (use get_daily_productivity_overview).

    Returns: `data` with `currently_tracking`, `tracked_item`, `account_stats`,
    `kudos_info`, `tracking_status`, `suggestion`.
    Hits /me/currentlyTrackedItem + /me + /me/kudos.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()

        # Get currently tracked item
        tracked_item = api_client.get_currently_tracked_item()

        # Get account info which may include time tracking stats
        account = api_client.get_account_info()

        # Get kudos info for productivity rewards
        kudos = api_client.get_kudos_info()

        is_tracking = tracked_item and "message" not in tracked_item

        result = {
            "currently_tracking": is_tracking,
            "tracked_item": tracked_item if is_tracking else None,
            "account_stats": account,
            "kudos_info": kudos,
            "tracking_status": "Active" if is_tracking else "Not tracking",
            "suggestion": "Start tracking a task to measure productivity"
            if not is_tracking
            else f"Currently tracking: {tracked_item.get('title', 'Unknown task')}",
        }

        return create_simple_response(
            data=result,
            summary_text="Active time tracking"
            if is_tracking
            else "No active time tracking",
            api_endpoint="/me/currentlyTrackedItem + /me + /me/kudos",
            api_calls_made=3,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get time tracking summary")
        return create_error_response(
            e, "/me/currentlyTrackedItem + /me + /me/kudos", debug, start_time
        )


@mcp.tool()
async def get_completed_tasks(debug: bool = False) -> StandardResponse:
    """Return tasks completed in the last 7 days, grouped by project (fixed window).

    Use when: user asks for "recent wins", "what did I get done this week".
    Don't use for: a specific calendar day (use get_completed_tasks_for_date) or arbitrary
    ranges / aggregated metrics (use get_productivity_summary_for_time_range).

    Returns: `data` with `completed_tasks`, `total_completed`, grouped breakdown.
    Hits /doneItems once per day (7 calls).
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = get_completed_tasks_impl(api_client)

        return create_simple_response(
            data=result,
            summary_text=f"Retrieved {result.get('total_completed', 0)} completed tasks from past 7 days",
            api_endpoint="/doneItems",
            api_calls_made=7,  # One call per day for 7 days
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get completed tasks")
        return create_error_response(e, "/doneItems", debug, start_time)


@mcp.tool()
async def get_productivity_summary_for_time_range(
    days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    debug: bool = False,
) -> StandardResponse:
    """Aggregate completed-task metrics over an arbitrary date range.

    Use when: user asks for productivity over a custom window (last 30 days, June 1-10,
    "this month").
    Don't use for: a single day (use get_completed_tasks_for_date) or the fixed past-7-day
    window with grouping (use get_completed_tasks).

    Args:
        days: Rolling window size in days, counted backwards from today. Ignored if
            start_date is given. Defaults to 7.
        start_date: Window start as "YYYY-MM-DD" (overrides `days`).
        end_date: Window end as "YYYY-MM-DD". Defaults to today when start_date is set.

    Examples: days=30; start_date="2025-06-01", end_date="2025-06-10";
    start_date="2025-06-01" alone.

    Returns: `data` with totals, per-day breakdown, productivity insights.
    Hits /doneItems once per day in the range.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = get_productivity_summary_for_time_range_impl(
            api_client, days, start_date, end_date
        )

        # Estimate API calls based on date range
        estimated_days = days or 7
        if start_date and end_date:
            start = datetime.strptime(start_date, "%Y-%m-%d")
            end = datetime.strptime(end_date, "%Y-%m-%d")
            estimated_days = (end - start).days + 1

        return create_simple_response(
            data=result,
            summary_text=f"Retrieved productivity summary for {estimated_days} days",
            api_endpoint="/doneItems",
            api_calls_made=estimated_days,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get productivity summary")
        return create_error_response(e, "/doneItems", debug, start_time)


@mcp.tool()
async def get_completed_tasks_for_date(
    date: str, debug: bool = False
) -> StandardResponse:
    """Return tasks marked done on one specific calendar date, grouped by project.

    Use when: user asks "what did I finish on YYYY-MM-DD" — exact-day queries.
    Don't use for: rolling windows (use get_completed_tasks for past 7 days, or
    get_productivity_summary_for_time_range for arbitrary ranges).

    Args:
        date: Calendar date as "YYYY-MM-DD" (e.g. "2025-06-13").

    Returns: `data` with `total_completed`, `completed_by_project`, `unassigned_completed`,
    `all_completed`. Hits /doneItems once with date filter.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        completed_items = api_client.get_done_items(date=date)

        # Group by project for better organization
        by_project: dict[str, list[dict[str, Any]]] = {}
        unassigned: list[dict[str, Any]] = []

        for item in completed_items:
            parent_id = item.get("parentId", "unassigned")

            if parent_id == "unassigned":
                unassigned.append(item)
            else:
                if parent_id not in by_project:
                    by_project[parent_id] = []
                by_project[parent_id].append(item)

        result = {
            "date": date,
            "total_completed": len(completed_items),
            "completed_by_project": by_project,
            "unassigned_completed": unassigned,
            "project_count": len(by_project),
            "unassigned_count": len(unassigned),
            "all_completed": completed_items,
            "source": f"Efficiently filtered from /doneItems?date={date}",
        }

        return create_simple_response(
            data=result,
            summary_text=f"Retrieved {len(completed_items)} completed tasks for {date}",
            api_endpoint="/doneItems",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get completed tasks for %s", date)
        return create_error_response(e, "/doneItems", debug, start_time)


@mcp.tool()
async def get_habits(debug: bool = False) -> StandardResponse:
    """List all habits as FULL CouchDB documents (title, target, recordType,
    period as 'day'/'week'/'month', units, history, db, ...).

    Marvin's /api/habits endpoint by itself only returns a reduced
    projection without title/target/db (and period as an int code), so
    this tool enriches each entry to the canonical doc shape:
      - If CouchDB direct access is configured, one Mango _find query.
      - Otherwise: /api/habits + per-habit /api/doc?id= (N+1 round trips).

    Use when: listing habits for the user (titles, _ids, targets, etc.).
    Don't use for:
      - Streak calculations -> use get_habit_streak (handles bucketing + targets).
      - Recording / undoing a habit -> use record_habit / undo_habit.

    Returns: `data.habits` (list of full docs).
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        habits = get_enriched_habits_impl(api_client)

        return create_simple_response(
            data={"habits": habits},
            summary_text=f"Retrieved {len(habits)} habits",
            api_endpoint="/habits",
            api_calls_made=1 if getattr(api_client, "has_couchdb", False)
            else 1 + len(habits),
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get habits")
        return create_error_response(e, "/habits", debug, start_time)


@mcp.tool()
async def get_habit(habit_id: str, debug: bool = False) -> StandardResponse:
    """Get one habit as the FULL CouchDB document (title, target, recordType,
    period as 'day'/'week'/'month', units, history, db, ...).

    Reads via /api/doc?id= so callers get the canonical doc shape (NOT the
    reduced /api/habit projection that omits title/target/db).

    Use when: you have a habit _id and need its title, target, recordType, history.
    Don't use for:
      - Streaks ("how many days in a row?") -> use get_habit_streak.
      - Recording / undoing -> use record_habit / undo_habit.

    Args:
        habit_id: Opaque CouchDB _id of the habit.

    Returns: `data.habit` (full doc).
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        habit = get_enriched_habit_impl(api_client, habit_id)

        return create_simple_response(
            data={"habit": habit},
            summary_text=f"Retrieved habit {habit_id}",
            api_endpoint="/doc",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get habit %s", habit_id)
        return create_error_response(e, "/doc", debug, start_time)


@mcp.tool()
async def record_habit(
    habit_id: str, value: int | None = None, debug: bool = False
) -> StandardResponse:
    """Record a habit occurrence for today (appends an entry to the habit's history).

    Use when: user reports they did the habit today ("I meditated", "ran 5km").
    Don't use for:
      - Undoing a recording -> use undo_habit.
      - Checking streak status -> use get_habit_streak.
      - Editing prior history entries -> not supported by this tool; use update_document.

    Args:
        habit_id: Opaque CouchDB _id of the habit.
        value: Numeric value for QUANTITATIVE habits (recordType=="number"), e.g. steps
            or minutes. Omit for BOOLEAN habits (recordType=="boolean") where each call
            counts as 1.

    Returns: `data.result` (raw payload from /updateHabit). Hits /updateHabit.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        habit_data: dict = {"habitId": habit_id, "action": "record"}
        if value is not None:
            habit_data["value"] = value
        result = api_client.update_habit(habit_data)

        return create_simple_response(
            data={"result": result},
            summary_text=f"Recorded habit {habit_id}",
            api_endpoint="/updateHabit",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to record habit %s", habit_id)
        return create_error_response(e, "/updateHabit", debug, start_time)


@mcp.tool()
async def undo_habit(habit_id: str, debug: bool = False) -> StandardResponse:
    """Remove the most recent recording from a habit's history (one undo per call).

    Use when: user mis-clicked or wants to revert a record_habit call.
    Don't use for:
      - Deleting a specific older history entry -> not supported by this tool.
      - Recording a new entry -> use record_habit.

    Args:
        habit_id: Opaque CouchDB _id of the habit.

    Returns: `data.result` (raw payload from /updateHabit). Hits /updateHabit.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = api_client.update_habit({"habitId": habit_id, "action": "undo"})

        return create_simple_response(
            data={"result": result},
            summary_text=f"Undid habit {habit_id}",
            api_endpoint="/updateHabit",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to undo habit %s", habit_id)
        return create_error_response(e, "/updateHabit", debug, start_time)


@mcp.tool()
async def get_habit_streak(
    habit_id: str,
    target_per_period: float | None = None,
    debug: bool = False,
) -> StandardResponse:
    """Compute current and longest streak for a habit from its history.

    This tool aggregates the habit's `history` array into per-period buckets
    (day / week / month, depending on the habit's own `period` setting) and
    returns the consecutive-fulfilled run ending today (`current_streak`)
    plus the longest such run anywhere in history (`longest_streak`).

    When to use this vs. `get_habit`:
    - `get_habit` returns only the raw CouchDB document. Use it when you
      need title, target, period, units, or the raw `history` array.
    - `get_habit_streak` is the right tool when the user asks about
      streaks, "how many days in a row", "longest streak", "did I do it
      today" — all of which require period bucketing and target comparison
      that the raw doc does not provide.

    Semantics:
    - A bucket counts as fulfilled when the sum of recorded values in that
      bucket is >= `target_per_period` (or the habit's own `target`).
    - For `recordType="boolean"` each recording contributes 1; for
      `recordType="number"` the recorded value is the count (may be float).
    - Weeks use ISO calendar weeks (Monday start), via
      `datetime.isocalendar()`.
    - "Today not yet recorded" does NOT break the streak — when the
      current period is not yet fulfilled we count consecutive fulfilled
      buckets starting one period back.

    Args:
        habit_id: The opaque CouchDB `_id` of the habit (same `habit_id`
            used by `record_habit` / `get_habit`).
        target_per_period: Optional override for the per-period target
            (float). Defaults to the habit's own `target`. Useful for
            "what would my streak be if I aimed for N per day?" queries.

    Returns (in `data`):
        habit_id, title, period ("day"|"week"|"month"), target,
        record_type, current_streak (int), longest_streak (int),
        last_fulfilled_bucket (tuple|None — shape depends on period:
        (Y,M,D) | (iso_year,iso_week) | (Y,M)), today_fulfilled (bool),
        today_value (float), total_records (int).
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = get_habit_streak_impl(api_client, habit_id, target_per_period)

        return create_simple_response(
            data=result,
            summary_text=(
                f"Habit '{result.get('title')}' current={result['current_streak']} "
                f"longest={result['longest_streak']}"
            ),
            api_endpoint="/habit",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to compute habit streak for %s", habit_id)
        return create_error_response(e, "/habit", debug, start_time)


@mcp.tool()
async def add_event(
    title: str,
    start: str,
    length_minutes: int,
    note: str | None = None,
    debug: bool = False,
) -> StandardResponse:
    """Add a calendar event (not a task) to Marvin.

    Use when: scheduling a meeting / blocked-time event the user wants on the calendar
    side of Marvin.
    Don't use for: tasks (use create_task) or Marvin time-blocks of the daily planner
    (use get_today_time_blocks / Marvin UI to manage those).

    Args:
        title: Event title (required).
        start: Start datetime — ISO 8601 ("2025-06-10T09:00:00") or a Unix millisecond
            timestamp Marvin's /addEvent accepts.
        length_minutes: Duration in minutes (converted to ms internally).
        note: Optional notes.

    Returns: `data.event` (created event payload). Hits /addEvent.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        event_data: dict = {
            "title": title,
            "start": start,
            "length": length_minutes * 60_000,
        }
        if note is not None:
            event_data["note"] = note
        result = api_client.add_event(event_data)

        return create_simple_response(
            data={"event": result},
            summary_text=f"Added event '{title}'",
            api_endpoint="/addEvent",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to add event '%s'", title)
        return create_error_response(e, "/addEvent", debug, start_time)


@mcp.tool()
async def get_today_time_blocks(
    date: str | None = None, debug: bool = False
) -> StandardResponse:
    """Get the time-blocks (planner blocks) scheduled for today or another date.

    Use when: user asks "what's my time block schedule", or you need the day's planner
    layout including durations.
    Don't use for: tasks scheduled on a day (use get_tasks / get_daily_productivity_overview).

    Args:
        date: Optional calendar date as "YYYY-MM-DD". Default = today.

    Returns: `data.time_blocks` (list). Hits /todayTimeBlocks.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        blocks = api_client.get_today_time_blocks(date)

        return create_simple_response(
            data={"time_blocks": blocks},
            summary_text=f"Retrieved {len(blocks)} time blocks",
            api_endpoint="/todayTimeBlocks",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to get today's time blocks")
        return create_error_response(e, "/todayTimeBlocks", debug, start_time)


@mcp.tool()
async def set_reminders(
    reminders: list[dict], debug: bool = False
) -> StandardResponse:
    """Register one or more reminders on tasks/events.

    Use when: user wants to be pinged at a future time about an item.
    Don't use for: removing reminders (use delete_reminders).

    Args:
        reminders: List of reminder dicts. Each follows the Marvin /reminder/set schema —
            typically `{"itemId": "<_id>", "time": <unix-ms>, ...}`. See the Marvin API docs
            for the full field set.

    Returns: `data.result` (raw payload). Hits /reminder/set.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = api_client.set_reminders(reminders)

        return create_simple_response(
            data={"result": result},
            summary_text=f"Set {len(reminders)} reminder(s)",
            api_endpoint="/reminder/set",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to set reminders")
        return create_error_response(e, "/reminder/set", debug, start_time)


@mcp.tool()
async def delete_reminders(
    reminder_ids: list[str], debug: bool = False
) -> StandardResponse:
    """Delete one or more reminders by their reminder _ids.

    Use when: cancelling pending notifications.
    Don't use for: creating reminders (use set_reminders).

    Args:
        reminder_ids: List of reminder _ids to remove, e.g. ["rem_abc","rem_def"].

    Returns: `data.result` (raw payload). Hits /reminder/delete.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = api_client.delete_reminders(reminder_ids)

        return create_simple_response(
            data={"result": result},
            summary_text=f"Deleted {len(reminder_ids)} reminder(s)",
            api_endpoint="/reminder/delete",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to delete reminders")
        return create_error_response(e, "/reminder/delete", debug, start_time)


@mcp.tool()
async def spend_reward_points(
    points: int, date: str, debug: bool = False
) -> StandardResponse:
    """Spend reward points (deducts from the balance — reward gamification).

    Workflow: balance grows via claim_reward_points (or auto-claims), is read via
    get_kudos_info, and is reduced here when the user redeems a reward.

    Side effect: decreases the user's point balance — NOT reversible by this tool. (To
    reverse a CLAIM specifically, use unclaim_reward_points; there is no direct "unspend".)

    Args:
        points: Positive int — number of points to deduct.
        date: Date the spend is recorded for, as "YYYY-MM-DD".

    Returns: `data.result` (raw payload). Hits /spendRewardPoints.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = api_client.spend_reward_points(points, date)

        return create_simple_response(
            data={"result": result},
            summary_text=f"Spent {points} reward points",
            api_endpoint="/spendRewardPoints",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to spend reward points")
        return create_error_response(e, "/spendRewardPoints", debug, start_time)


@mcp.tool()
async def unclaim_reward_points(
    item_id: str, date: str, debug: bool = False
) -> StandardResponse:
    """Reverse a previous claim_reward_points call for a task (refunds the points).

    Use when: a task completion was undone, or the wrong task was credited.
    Don't use for: spending points (use spend_reward_points) — semantics differ.

    Args:
        item_id: Opaque _id of the task the original claim was tied to.
        date: Date of the original claim, as "YYYY-MM-DD" (must match).

    Returns: `data.result` (raw payload). Hits /unclaimRewardPoints.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = api_client.unclaim_reward_points(item_id, date)

        return create_simple_response(
            data={"result": result},
            summary_text=f"Unclaimed reward points for task {item_id}",
            api_endpoint="/unclaimRewardPoints",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except Exception as e:
        logger.exception("Failed to unclaim reward points for task %s", item_id)
        return create_error_response(e, "/unclaimRewardPoints", debug, start_time)


@mcp.tool()
async def list_smart_lists(debug: bool = False) -> StandardResponse:
    """List all user-defined SmartLists with a summary (id, name, sort, groupBy, active clauses).

    Requires direct CouchDB access — set AMAZING_MARVIN_DB_URI, _DB_NAME,
    _DB_USER, _DB_PASSWORD in the deployment. The Marvin REST API does not
    expose SmartLists, so without CouchDB credentials this tool returns an
    error response (it does not crash the server).

    Only USER-DEFINED SmartLists are returned. System SmartLists (Today,
    Backlog, etc.) live elsewhere and are NOT included.

    Returns a list of summaries; use get_smart_list(id) to fetch the full
    document including raw filter clauses.
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        smart_lists = list_smart_lists_impl(api_client)
        logger.info("Listed %d SmartLists", len(smart_lists))
        return create_simple_response(
            data={"smart_lists": smart_lists, "count": len(smart_lists)},
            summary_text=f"Retrieved {len(smart_lists)} SmartLists",
            api_endpoint="CouchDB /_find db=SmartLists",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except ValueError as e:
        logger.warning("list_smart_lists rejected: %s", e)
        return create_error_response(e, "CouchDB /_find", debug, start_time)
    except Exception as e:
        logger.exception("Failed to list SmartLists")
        return create_error_response(e, "CouchDB /_find", debug, start_time)


@mcp.tool()
async def get_smart_list(smart_list_id: str, debug: bool = False) -> StandardResponse:
    """Read one SmartList document by id, returning its full raw definition.

    Prefer this over get_document for SmartList ids: it verifies that the
    document's `db` is `"SmartLists"` and rejects anything else. Use
    get_document only when you genuinely need a non-SmartList doc.

    The returned doc contains all layout fields (groupBy, sort, limit,
    oneRT, removeRedundancies, refill) and the raw filter clauses as
    top-level fields (e.g. itemType, parentId, planAhead, advanced).

    Requires AMAZING_MARVIN_FULL_ACCESS_TOKEN (uses /api/doc, not CouchDB).

    Args:
        smart_list_id: The SmartList's _id (opaque short string, no prefix)
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        doc = get_smart_list_impl(api_client, smart_list_id)
        logger.info("Retrieved SmartList %s", smart_list_id)
        return create_simple_response(
            data=doc,
            summary_text=f"Retrieved SmartList {doc.get('name', smart_list_id)!r}",
            api_endpoint=f"/doc?id={smart_list_id}",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except ValueError as e:
        logger.warning("get_smart_list rejected: %s", e)
        return create_error_response(e, "/doc", debug, start_time)
    except Exception as e:
        logger.exception("Failed to get SmartList %s", smart_list_id)
        return create_error_response(e, "/doc", debug, start_time)


@mcp.tool()
async def create_smart_list(
    name: str,
    fields: dict[str, Any] | None = None,
    debug: bool = False,
) -> StandardResponse:
    """Create a new user-defined SmartList (requires AMAZING_MARVIN_FULL_ACCESS_TOKEN).

    `name` is the only required field. All other fields are passed via the
    optional `fields` dict and must be in the whitelist (see below).
    Internal meta fields (_id, _rev, db, createdAt, fieldUpdates) are set
    automatically and must NOT be in `fields`.

    Writable LAYOUT/BEHAVIOR fields:
        - name: str (already provided as positional arg, do not repeat in `fields`)
        - groupBy: str | None  (e.g. "goalId", "parentId", or None)
        - sort: list[{"field": str, "dir": "asc"|"desc"}]
                e.g. [{"field": "day", "dir": "asc"}]
        - limit: int  (0 = unlimited)
        - oneRT: bool
        - removeRedundancies: bool
        - refill: str  (e.g. "auto")

    Writable FILTER-CLAUSE fields — set them as top-level keys in `fields`,
    each either `null` (clause off) or a dict `{"op": "<operator>", "val": <value>}`
    (some operators omit "val"):
        itemType, recurring, parentId, goalId, title, hasTime, created,
        day, dueDate, endDate, startDate, pledgeDate, firstScheduled,
        procrastinationCount, backburner, isStarred, isFrogged, labelIds,
        project, nextStep, planAhead, timeEstimate, timeBlock, advanced

    Examples:
        - parentId: {"op": "in", "val": "<uuid>"}
        - itemType: {"op": "task"}   (no val)
        - planAhead: {"op": "&thisWeek"}
        - advanced: {"op": "y", "val": "*hasChildren *false == *type:project &&"}
          (RPN expression — Marvin "Advanced filter" syntax)

    Args:
        name: Display name for the SmartList (required)
        fields: Optional dict of additional writable fields (see lists above)
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = create_smart_list_impl(api_client, name, **(fields or {}))
        logger.info("Created SmartList %r", name)
        return create_simple_response(
            data=result,
            summary_text=f"Created SmartList {name!r}",
            api_endpoint="/doc/create",
            api_calls_made=1,
            debug=debug,
            start_time=start_time,
        )
    except ValueError as e:
        logger.warning("create_smart_list rejected: %s", e)
        return create_error_response(e, "/doc/create", debug, start_time)
    except Exception as e:
        logger.exception("Failed to create SmartList %r", name)
        return create_error_response(e, "/doc/create", debug, start_time)


@mcp.tool()
async def update_smart_list(
    smart_list_id: str,
    changes: dict[str, Any],
    debug: bool = False,
) -> StandardResponse:
    """Update fields on an existing SmartList (requires AMAZING_MARVIN_FULL_ACCESS_TOKEN).

    Pass only the fields you want to change in `changes`. The whitelist is
    enforced — any non-writable key raises an error before the API call.
    Before mutating, the tool reads the document and verifies `db == 'SmartLists'`
    to refuse updates on non-SmartList docs (use update_document for those).

    Writable LAYOUT/BEHAVIOR fields:
        name, groupBy, sort, limit, oneRT, removeRedundancies, refill

    Writable FILTER-CLAUSE fields (top-level, each `null` to DISABLE the
    clause or `{"op": "...", "val": ...}` to set/replace it):
        itemType, recurring, parentId, goalId, title, hasTime, created,
        day, dueDate, endDate, startDate, pledgeDate, firstScheduled,
        procrastinationCount, backburner, isStarred, isFrogged, labelIds,
        project, nextStep, planAhead, timeEstimate, timeBlock, advanced

    To REMOVE an active clause, set it to `null` in `changes`
    (e.g. {"parentId": None}). To ADD/CHANGE one, pass the full clause dict.

    See create_smart_list for clause examples (parentId-in, planAhead,
    advanced RPN, etc.).

    Args:
        smart_list_id: The SmartList's _id
        changes: Dict of writable field → new value
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = update_smart_list_impl(api_client, smart_list_id, **changes)
        logger.info(
            "Updated SmartList %s with %d field(s)", smart_list_id, len(changes)
        )
        return create_simple_response(
            data=result,
            summary_text=(
                f"Updated SmartList {smart_list_id} with {len(changes)} field(s)"
            ),
            api_endpoint="/doc/update",
            api_calls_made=2,  # get_document (safety) + update_document
            debug=debug,
            start_time=start_time,
        )
    except ValueError as e:
        logger.warning("update_smart_list rejected: %s", e)
        return create_error_response(e, "/doc/update", debug, start_time)
    except Exception as e:
        logger.exception("Failed to update SmartList %s", smart_list_id)
        return create_error_response(e, "/doc/update", debug, start_time)


@mcp.tool()
async def delete_smart_list(
    smart_list_id: str, debug: bool = False
) -> StandardResponse:
    """Permanently delete a SmartList (requires AMAZING_MARVIN_FULL_ACCESS_TOKEN).

    Safety pre-flight: reads the document first and refuses to delete unless
    `db == 'SmartLists'`. Other document types (Tasks, Goals, Habits, etc.)
    raise an error and are NOT deleted — use delete_document for those after
    its own task/container safety checks.

    Note: SmartLists are pure configuration — deleting one does NOT delete
    any tasks/projects it referenced.

    Args:
        smart_list_id: The SmartList's _id
    """
    start_time = time.time()
    try:
        api_client = create_api_client()
        result = delete_smart_list_impl(api_client, smart_list_id)
        logger.info("Deleted SmartList %s", smart_list_id)
        return create_simple_response(
            data=result,
            summary_text=f"Deleted SmartList {smart_list_id}",
            api_endpoint="/doc/delete",
            api_calls_made=2,  # get_document (safety) + delete_document
            debug=debug,
            start_time=start_time,
        )
    except ValueError as e:
        logger.warning("delete_smart_list rejected: %s", e)
        return create_error_response(e, "/doc/delete", debug, start_time)
    except Exception as e:
        logger.exception("Failed to delete SmartList %s", smart_list_id)
        return create_error_response(e, "/doc/delete", debug, start_time)


# WRITABLE_CLAUSE_FIELDS is re-exported here so introspection / debugging
# helpers (and a small number of imports) can reach it without diving into
# the smartlists module directly.
_SMART_LIST_CLAUSE_FIELDS = WRITABLE_CLAUSE_FIELDS


def start():
    """Start the MCP server.

    MCP_TRANSPORT selects the transport:
      - "stdio" (default): subprocess pipe — used by Smithery, mcp-proxy as command.
      - "http": FastMCP streamable HTTP at /mcp/.
      - "sse":  FastMCP Server-Sent-Events at /sse — widest client compatibility,
                preferred when fronting via tbxark/mcp-proxy `url` backends.
    """
    transport = os.getenv("MCP_TRANSPORT", "stdio").lower()

    if transport in ("http", "sse"):
        host = os.getenv("MCP_HOST", "0.0.0.0")
        port = int(os.getenv("MCP_PORT", "8000"))
        mcp.run(transport=transport, host=host, port=port)
    else:
        mcp.run()  # Default STDIO transport


if __name__ == "__main__":
    start()
