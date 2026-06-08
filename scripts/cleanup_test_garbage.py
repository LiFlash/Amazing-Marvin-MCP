"""Delete pytest-generated Test-Project and Test-Task docs from Marvin.

Identifies:
- Projects whose title exactly matches  ^Pytest Test Project - \\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2}$
- Tasks whose title exactly matches     ^Test Task \\d+$
- Tasks whose title is exactly           "Test Scheduling"  --> SKIP (looks user-owned, done=True)
- The category "Auftankaktivitäten testen" --> SKIP

Run with --dry-run to print only.
"""
import re
import sys
from amazing_marvin_mcp.api import create_api_client

PROJECT_PAT = re.compile(r"^Pytest Test Project - \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
TASK_PAT = re.compile(r"^Test Task \d+$")
PROTECTED_TITLES = {"Auftankaktivitäten testen", "Test Scheduling"}


def main(dry_run: bool):
    c = create_api_client()
    projects = c.get_projects()

    proj_to_delete = [
        p for p in projects
        if PROJECT_PAT.match(p.get("title", ""))
        and p.get("title") not in PROTECTED_TITLES
    ]
    proj_ids = {p["_id"] for p in proj_to_delete}

    if not c.has_couchdb:
        print("ERROR: CouchDB credentials missing — cannot enumerate child tasks.")
        sys.exit(1)

    all_tasks = c.find_docs({"db": "Tasks"}, limit=500)
    tasks_to_delete = [
        t for t in all_tasks
        if (
            (TASK_PAT.match(t.get("title", ""))
             or t.get("parentId") in proj_ids)
            and t.get("title") not in PROTECTED_TITLES
        )
    ]
    task_ids_to_delete = {t["_id"] for t in tasks_to_delete}

    print(f"Projects to delete: {len(proj_to_delete)}")
    for p in proj_to_delete:
        print(f"  - {p['_id']}  {p.get('title')}")
    print()
    print(f"Tasks to delete: {len(tasks_to_delete)}")
    for t in tasks_to_delete[:50]:
        print(f"  - {t['_id']}  {t.get('title')}  parent={t.get('parentId')}")
    if len(tasks_to_delete) > 50:
        print(f"  ... and {len(tasks_to_delete)-50} more")
    print()

    if dry_run:
        print("DRY RUN — nothing deleted.")
        return

    failures = []

    # Delete tasks first, then projects (avoids orphan child blocking)
    for tid in task_ids_to_delete:
        try:
            c.delete_document(tid)
        except Exception as e:
            failures.append((tid, "task", str(e)))

    for pid in proj_ids:
        try:
            c.delete_document(pid)
        except Exception as e:
            failures.append((pid, "project", str(e)))

    print(f"Deleted {len(task_ids_to_delete)} tasks and {len(proj_ids)} projects.")
    if failures:
        print(f"Failures: {len(failures)}")
        for f in failures:
            print(" -", f)


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv or "-n" in sys.argv
    main(dry)
