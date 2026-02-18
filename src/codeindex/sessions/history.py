"""
Session history — change log with transcript linking.
"""

from __future__ import annotations

from typing import Any, Optional

from ..store.db import Database
from ..core.differ import Differ


class SessionHistory:
    """Query session change history."""

    def __init__(self, db: Database, differ: Differ):
        self.db = db
        self.differ = differ

    def changes_since(self, session_id: int) -> list[dict[str, Any]]:
        """Get all file changes since a session."""
        return self.differ.changes_since_session(session_id)

    def current_changes(self) -> list[dict[str, Any]]:
        """Get files changed since last index update."""
        return self.differ.detect_current_changes()

    def record_snapshot(self, session_id: int) -> list[dict[str, Any]]:
        """Record current changes to the session's change log."""
        return self.differ.record_changes(session_id)
