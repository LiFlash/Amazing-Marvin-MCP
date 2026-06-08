import logging
from typing import Any

import requests

from .config import get_settings

logger = logging.getLogger(__name__)


def create_api_client() -> "MarvinAPIClient":
    """Create API client with settings."""
    settings = get_settings()
    return MarvinAPIClient(
        api_key=settings.amazing_marvin_api_key,
        full_access_token=settings.amazing_marvin_full_access_token,
        db_uri=settings.amazing_marvin_db_uri,
        db_name=settings.amazing_marvin_db_name,
        db_user=settings.amazing_marvin_db_user,
        db_password=settings.amazing_marvin_db_password,
    )


class MarvinAPIClient:
    """API client for Amazing Marvin"""

    def __init__(
        self,
        api_key: str,
        full_access_token: str | None = None,
        db_uri: str = "",
        db_name: str = "",
        db_user: str = "",
        db_password: str = "",
    ):
        """
        Initialize the API client with the API key

        Args:
            api_key: Amazing Marvin API key
            full_access_token: Optional full-access token for CRUD operations
            db_uri: CouchDB base URI (e.g. https://user.cloudant.com)
            db_name: CouchDB database name
            db_user: CouchDB username for basic auth
            db_password: CouchDB password for basic auth
        """
        self.api_key = api_key
        self.base_url = "https://serv.amazingmarvin.com/api"  # Removed v1 from URL
        self.headers = {"X-API-Token": api_key}
        self.full_access_token = full_access_token
        self.full_access_headers: dict[str, str] | None = (
            {"X-Full-Access-Token": full_access_token} if full_access_token else None
        )
        # Strip trailing slash to prevent double-slash in URLs like //{db}/_find.
        self.db_uri = db_uri.rstrip("/")
        self.db_name = db_name
        self.db_user = db_user
        self.db_password = db_password

    @property
    def has_full_access(self) -> bool:
        return bool(self.full_access_token)

    @property
    def has_couchdb(self) -> bool:
        """True if all four CouchDB credentials are configured."""
        return bool(self.db_uri and self.db_name and self.db_user and self.db_password)

    def find_docs(
        self,
        selector: dict,
        fields: list[str] | None = None,
        limit: int = 500,
    ) -> list[dict]:
        """Query CouchDB directly using a Mango selector.

        Requires all four CouchDB credentials to be configured
        (AMAZING_MARVIN_DB_URI, _DB_NAME, _DB_USER, _DB_PASSWORD).

        Args:
            selector: CouchDB Mango selector dict (e.g. {"db": "SmartLists"})
            fields: Optional list of fields to return per document.
                    If None, all fields are returned.
            limit: Maximum number of documents to return.

        Returns:
            List of matching documents.

        Note: Hard limit of 500 docs per query — no pagination is implemented.
        Callers must not rely on completeness for large result sets (Open Question 7).
        """
        if not self.has_couchdb:
            raise ValueError(
                "CouchDB credentials not fully configured. "
                "Set AMAZING_MARVIN_DB_URI, AMAZING_MARVIN_DB_NAME, "
                "AMAZING_MARVIN_DB_USER, and AMAZING_MARVIN_DB_PASSWORD."
            )

        url = f"{self.db_uri}/{self.db_name}/_find"
        body: dict[str, Any] = {"selector": selector, "limit": limit}
        if fields is not None:
            body["fields"] = fields

        # NB: Selectors may contain user data when find_docs is used beyond SmartLists/Habits.
        # Keep at DEBUG level only.
        logger.debug("CouchDB _find POST to %s with selector %s", url, selector)
        try:
            response = requests.post(
                url,
                json=body,
                auth=(self.db_user, self.db_password),
            )
            response.raise_for_status()
            return response.json().get("docs", [])
        except requests.exceptions.HTTPError:
            logger.exception("CouchDB HTTP error querying %s", url)
            raise
        except requests.exceptions.RequestException:
            logger.exception("CouchDB request error querying %s", url)
            raise

    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: dict | None = None,
        use_full_access: bool = False,
    ) -> Any:
        """Make a request to the API"""
        if use_full_access:
            if not self.has_full_access:
                raise ValueError(
                    "Full-access token not configured. Set AMAZING_MARVIN_FULL_ACCESS_TOKEN."
                )
            headers = self.full_access_headers
        else:
            headers = self.headers
        url = f"{self.base_url}{endpoint}"
        logger.debug("Making %s request to %s", method, url)

        try:
            if method.lower() == "get":
                response = requests.get(url, headers=headers)
            elif method.lower() == "post":
                response = requests.post(url, headers=headers, json=data)
            elif method.lower() == "put":
                response = requests.put(url, headers=headers, json=data)
            elif method.lower() == "delete":
                response = requests.delete(url, headers=headers)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status()

            # Handle 204 No Content responses
            no_content_status = 204
            if response.status_code == no_content_status or not response.content:
                return {}

            return response.json()
        except requests.exceptions.HTTPError:
            logger.exception("HTTP error")
            raise
        except requests.exceptions.RequestException:
            logger.exception("Request error")
            raise

    def get_document(self, doc_id: str) -> dict:
        """Read any Marvin document by ID (requires full-access token)."""
        return self._make_request("get", f"/doc?id={doc_id}", use_full_access=True)

    def update_document(self, item_id: str, setters: list[dict] | dict) -> dict:
        """Update fields on any Marvin document (requires full-access token).

        Args:
            item_id: The document's _id
            setters: Either a list of {"key":…,"val":…} dicts (from build_setters) or
                     a plain dict of field→value pairs (converted to wire format internally).
        """
        if isinstance(setters, list):
            wire_setters = setters
        else:
            wire_setters = [{"key": k, "val": v} for k, v in setters.items()]
        return self._make_request(
            "post",
            "/doc/update",
            data={"itemId": item_id, "setters": wire_setters},
            use_full_access=True,
        )

    def create_document(self, doc_data: dict) -> dict:
        """Create a raw document. Warning: can create malformed tasks. Not exposed as MCP tool."""
        return self._make_request(
            "post", "/doc/create", data={"doc": doc_data}, use_full_access=True
        )

    def delete_document(self, item_id: str) -> dict:
        """Permanently delete a document."""
        return self._make_request(
            "post", "/doc/delete", data={"itemId": item_id}, use_full_access=True
        )

    def get_tasks(self, date: str | None = None) -> list[dict]:
        """Get all tasks and projects (use /todayItems or /dueItems for scheduled/due, or /children for subtasks)"""
        # The Marvin API does not provide a /tasks endpoint. Use /todayItems for scheduled items, /dueItems for due, or /children for subtasks.
        endpoint = "/todayItems"
        if date:
            endpoint += f"?date={date}"
        return self._make_request("get", endpoint)

    def get_task(self, task_id: str) -> dict:
        """Get a specific task by ID (delegates to get_document, requires full-access token)."""
        return self.get_document(task_id)

    def get_projects(self) -> list[dict]:
        """
        Get all projects (as categories with type 'project').

        Note: "Work" and "Personal" are default projects created for most users.
        """
        categories = self.get_categories()
        return [cat for cat in categories if cat.get("type") == "project"]

    def get_categories(self) -> list[dict]:
        """Get all categories"""
        return self._make_request("get", "/categories")

    def get_labels(self) -> list[dict]:
        """Get all labels"""
        return self._make_request("get", "/labels")

    def get_due_items(self) -> list[dict]:
        """Get all due items (experimental endpoint)"""
        return self._make_request("get", "/dueItems")

    def get_done_items(self, date: str | None = None) -> list[dict]:
        """Get completed/done items, optionally filtered by completion date

        Args:
            date: Optional date in YYYY-MM-DD format to filter by completion date.
                 If not provided, defaults to today's completed items.

        Returns:
            List of completed items, filtered by completion date if specified
        """
        endpoint = "/doneItems"
        if date:
            endpoint += f"?date={date}"
        return self._make_request("get", endpoint)

    def get_all_tasks_for_date(self, date: str) -> list[dict]:
        """Get all tasks for a specific date, including completed ones.

        Args:
            date: Date in YYYY-MM-DD format

        Returns:
            List of tasks for that date (both completed and pending)
        """
        try:
            # Try different approaches to get completed tasks
            result = []

            # 1. Try todayItems with date parameter
            today_items = self._make_request("get", f"/todayItems?date={date}")
            result.extend(today_items)

            # 2. Try any additional endpoints that might have completed tasks
            # The API might have other ways to access completed items
        except Exception as e:
            logger.warning("Could not get tasks for date %s: %s", date, e)
            return []
        else:
            return result

    def get_children(self, parent_id: str) -> list[dict]:
        """Get child tasks of a specific parent task or project (experimental endpoint)"""
        try:
            return self._make_request("get", f"/children?parentId={parent_id}")
        except requests.exceptions.HTTPError as e:
            not_found_status = 404
            if e.response.status_code == not_found_status:
                logger.warning(
                    "Children endpoint not available for parent %s", parent_id
                )
                return []
            raise

    def create_task(self, task_data: dict) -> dict:
        """Create a new task (uses /addTask endpoint)"""
        return self._make_request("post", "/addTask", data=task_data)

    def mark_task_done(self, item_id: str, timezone_offset: int = 0) -> dict:
        """Mark a task as done (experimental endpoint)"""
        return self._make_request(
            "post",
            "/markDone",
            data={"itemId": item_id, "timeZoneOffset": timezone_offset},
        )

    def test_api_connection(self) -> str:
        """Test API connection and credentials"""
        url = f"{self.base_url}/test"
        try:
            response = requests.post(url, headers=self.headers)
            response.raise_for_status()
            return response.text.strip()  # Returns "OK" as plain text
        except requests.exceptions.RequestException:
            logger.exception("API connection test failed")
            raise

    def start_time_tracking(self, task_id: str) -> dict:
        """Start time tracking for a task (experimental endpoint)"""
        return self._make_request(
            "post", "/track", data={"taskId": task_id, "action": "START"}
        )

    def stop_time_tracking(self, task_id: str) -> dict:
        """Stop time tracking for a task (experimental endpoint)"""
        return self._make_request(
            "post", "/track", data={"taskId": task_id, "action": "STOP"}
        )

    def get_time_tracks(self, task_ids: list[str]) -> dict:
        """Get time tracking data for specific tasks (experimental endpoint)"""
        return self._make_request("post", "/tracks", data={"taskIds": task_ids})

    def claim_reward_points(self, points: int, item_id: str, date: str) -> dict:
        """Claim reward points for completing a task"""
        return self._make_request(
            "post",
            "/claimRewardPoints",
            data={"points": points, "itemId": item_id, "date": date},
        )

    def get_kudos_info(self) -> dict:
        """Get kudos information"""
        return self._make_request("get", "/kudos")

    def get_goals(self) -> list[dict]:
        """Get all goals"""
        return self._make_request("get", "/goals")

    def get_account_info(self) -> dict:
        """Get account information"""
        return self._make_request("get", "/me")

    def get_currently_tracked_item(self) -> dict:
        """Get currently tracked item"""
        result = self._make_request("get", "/trackedItem")
        if not result:
            return {"message": "No item currently being tracked"}
        return result

    def create_project(self, project_data: dict) -> dict:
        """Create a new project (experimental endpoint)"""
        return self._make_request("post", "/addProject", data=project_data)

    def update_task(self, item_id: str, setters: list[dict] | dict) -> dict:
        """Update a task (delegates to update_document, requires full-access token)."""
        return self.update_document(item_id, setters)

    def add_event(self, event_data: dict) -> dict:
        """Add a calendar event."""
        return self._make_request("post", "/addEvent", data=event_data)

    def get_today_time_blocks(self, date: str | None = None) -> list[dict]:
        """Get time blocks for today (or a specific date)."""
        endpoint = "/todayTimeBlocks"
        if date:
            endpoint += f"?date={date}"
        return self._make_request("get", endpoint)

    def get_habits(self) -> list[dict]:
        """Get all habits."""
        return self._make_request("get", "/habits")

    def get_habit(self, habit_id: str) -> dict:
        """Get a specific habit by ID."""
        return self._make_request("get", f"/habit?id={habit_id}")

    def update_habit(self, habit_data: dict) -> dict:
        """Update/record a habit."""
        return self._make_request("post", "/updateHabit", data=habit_data)

    def set_reminders(self, reminders: list[dict]) -> dict:
        """Set reminders."""
        return self._make_request("post", "/reminder/set", data={"reminders": reminders})

    def delete_reminders(self, reminder_ids: list[str]) -> dict:
        """Delete reminders by ID."""
        return self._make_request(
            "post", "/reminder/delete", data={"reminderIds": reminder_ids}
        )

    def unclaim_reward_points(self, item_id: str, date: str) -> dict:
        """Unclaim previously claimed reward points."""
        return self._make_request(
            "post",
            "/unclaimRewardPoints",
            data={"itemId": item_id, "date": date},
        )

    def spend_reward_points(self, points: int, date: str) -> dict:
        """Spend reward points."""
        return self._make_request(
            "post",
            "/spendRewardPoints",
            data={"points": points, "date": date},
        )
