"""
Change detection between sessions.

Compares current file hashes against stored state to find what changed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..store.db import Database
from ..store.models import ChangeLog
from .indexer import Indexer, compute_file_hash


class Differ:
    """Detect file changes since last session or index."""

    def __init__(self, db: Database, indexer: Indexer):
        self.db = db
        self.indexer = indexer

    def changes_since_session(self, session_id: int) -> list[dict]:
        """Return files changed since a specific session started."""
        return self.db.get_session_changes(session_id)

    def detect_current_changes(self) -> list[dict]:
        """Compare current files on disk to what's in the index.

        Returns list of {rel_path, change_type, old_hash, new_hash}.
        """
        files = self.indexer.discover_files()
        current_paths = {rel for _, rel in files}
        changes = []

        # Check for modified/new files
        for abs_path, rel_path in files:
            existing = self.db.get_file_by_path(rel_path)
            file_hash = compute_file_hash(abs_path)

            if existing is None:
                changes.append({
                    "rel_path": rel_path,
                    "change_type": "added",
                    "old_hash": None,
                    "new_hash": file_hash,
                })
            elif existing.file_hash != file_hash:
                changes.append({
                    "rel_path": rel_path,
                    "change_type": "modified",
                    "old_hash": existing.file_hash,
                    "new_hash": file_hash,
                })

        # Check for deleted files
        for existing in self.db.list_files():
            if existing.rel_path not in current_paths:
                changes.append({
                    "rel_path": existing.rel_path,
                    "change_type": "deleted",
                    "old_hash": existing.file_hash,
                    "new_hash": None,
                })

        return changes

    def record_changes(self, session_id: int, changes: Optional[list[dict]] = None) -> list[dict]:
        """Record current changes to the change log for a session."""
        if changes is None:
            changes = self.detect_current_changes()

        for c in changes:
            f = self.db.get_file_by_path(c["rel_path"])
            if f:
                self.db.insert_change(ChangeLog(
                    session_id=session_id,
                    file_id=f.file_id,
                    change_type=c["change_type"],
                    old_hash=c.get("old_hash"),
                    new_hash=c.get("new_hash"),
                ))

        return changes
