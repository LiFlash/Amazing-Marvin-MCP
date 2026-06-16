"""Drift guard for the conftest cleanup patterns.

History: the conftest cleanup fixture once hard-coded ``^Test Task \\d+$``
which never matched the titles the live tests actually produce
(``Pytest Test Task - <timestamp>``). Junk piled up in Marvin for months
because nobody checked the two sides agreed.

These tests build a title using the **same constants and timestamp format
the live test fixtures use** (imported from tests/test_api.py) and assert
the conftest patterns match. If anyone changes the prefix or timestamp
format on one side and forgets the other, this test fails loudly.
"""

from datetime import datetime

from tests.conftest import _PROJECT_PAT, _TASK_PAT
from tests.test_api import (
    PYTEST_PROJECT_TITLE_PREFIX,
    PYTEST_TASK_TITLE_PREFIX,
    PYTEST_TIMESTAMP_FORMAT,
)


def _sample_task_title() -> str:
    return f"{PYTEST_TASK_TITLE_PREFIX}{datetime.now().strftime(PYTEST_TIMESTAMP_FORMAT)}"


def _sample_project_title() -> str:
    return f"{PYTEST_PROJECT_TITLE_PREFIX}{datetime.now().strftime(PYTEST_TIMESTAMP_FORMAT)}"


def test_conftest_task_pattern_matches_actual_test_titles():
    title = _sample_task_title()
    assert _TASK_PAT.match(title), (
        f"conftest._TASK_PAT does not match the title pattern that "
        f"test_api.py actually produces: {title!r}"
    )


def test_conftest_project_pattern_matches_actual_test_titles():
    title = _sample_project_title()
    assert _PROJECT_PAT.match(title), (
        f"conftest._PROJECT_PAT does not match the title pattern that "
        f"test_api.py actually produces: {title!r}"
    )


def test_conftest_task_pattern_rejects_unrelated_titles():
    # Sanity check on the other direction: the pattern must NOT match a
    # user-owned task that just happens to start with "Test". This is
    # what _PROTECTED_TITLES backstops, but the pattern itself should
    # already be tight enough.
    assert not _TASK_PAT.match("Auftankaktivitäten testen")
    assert not _TASK_PAT.match("Test Task 5")  # the old (wrong) pattern's shape
    assert not _TASK_PAT.match("Pytest Test Task - tomorrow")  # not a real timestamp
