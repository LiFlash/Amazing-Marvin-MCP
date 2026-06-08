"""Pytest session hooks.

The live integration tests in ``tests/test_api.py`` create real
``Pytest Test Project ...`` projects and ``Test Task N`` children in
Amazing Marvin via the REST API. They had no teardown originally, so
junk would pile up in the user's Marvin account. This fixture runs
once per session, snapshots the set of pytest-style project titles
*before* the run, and after the run deletes anything that matches the
pytest pattern but didn't exist at start.

If CouchDB credentials aren't configured the fixture falls back to
title-based cleanup via ``get_projects`` only (no task-by-parent
sweep). Without a full-access token, cleanup is silently skipped.
"""

from __future__ import annotations

import logging
import re

import pytest

logger = logging.getLogger(__name__)

_PROJECT_PAT = re.compile(r"^Pytest Test Project - \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
_TASK_PAT = re.compile(r"^Test Task \d+$")
# Real user-created items that LOOK pytest-y but must never be deleted.
_PROTECTED_TITLES = {"Auftankaktivitäten testen", "Test Scheduling"}


def _matches_pytest_project(doc: dict) -> bool:
    title = doc.get("title", "")
    return bool(_PROJECT_PAT.match(title)) and title not in _PROTECTED_TITLES


def _matches_pytest_task(doc: dict) -> bool:
    title = doc.get("title", "")
    return bool(_TASK_PAT.match(title)) and title not in _PROTECTED_TITLES


@pytest.fixture(scope="session", autouse=True)
def _cleanup_pytest_created_docs():
    """Auto-delete pytest-created Test-Project / Test-Task docs at session end."""
    try:
        from amazing_marvin_mcp.api import create_api_client
    except ImportError:
        yield
        return

    try:
        client = create_api_client()
    except Exception as e:  # noqa: BLE001
        logger.debug("Cleanup fixture: cannot create API client (%s) — skipping.", e)
        yield
        return

    if not getattr(client, "full_access_token", None):
        logger.debug("Cleanup fixture: no full-access token — skipping.")
        yield
        return

    # Snapshot existing pytest-style projects BEFORE the run so we never
    # touch ones that already lived in Marvin (e.g. user-created jokes).
    try:
        pre_existing_project_ids = {
            p["_id"]
            for p in client.get_projects()
            if _matches_pytest_project(p)
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("Cleanup fixture: pre-snapshot failed (%s) — skipping.", e)
        yield
        return

    yield

    # Post-run sweep
    try:
        current_projects = client.get_projects()
    except Exception as e:  # noqa: BLE001
        logger.warning("Cleanup fixture: post-snapshot get_projects failed: %s", e)
        return

    new_project_ids = {
        p["_id"]
        for p in current_projects
        if _matches_pytest_project(p) and p["_id"] not in pre_existing_project_ids
    }

    task_ids_to_delete: set[str] = set()
    if getattr(client, "has_couchdb", False):
        try:
            all_tasks = client.find_docs({"db": "Tasks"}, limit=500)
        except Exception as e:  # noqa: BLE001
            logger.warning("Cleanup fixture: find_docs failed: %s", e)
            all_tasks = []
        task_ids_to_delete = {
            t["_id"]
            for t in all_tasks
            if (_matches_pytest_task(t) or t.get("parentId") in new_project_ids)
            and t.get("title") not in _PROTECTED_TITLES
        }

    deleted_tasks = 0
    deleted_projects = 0
    for tid in task_ids_to_delete:
        try:
            client.delete_document(tid)
            deleted_tasks += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Cleanup fixture: failed to delete task %s: %s", tid, e)

    for pid in new_project_ids:
        try:
            client.delete_document(pid)
            deleted_projects += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("Cleanup fixture: failed to delete project %s: %s", pid, e)

    if deleted_tasks or deleted_projects:
        print(
            f"\n[pytest-cleanup] deleted {deleted_tasks} test tasks, "
            f"{deleted_projects} test projects from Marvin."
        )
