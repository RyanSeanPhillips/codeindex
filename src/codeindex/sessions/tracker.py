"""
Session tracker — start/end sessions, link to transcripts.
"""

from __future__ import annotations

from typing import Any, Optional

from ..store.db import Database
from ..store.models import Session


class SessionTracker:
    """Manage coding sessions with transcript linking."""

    def __init__(self, db: Database):
        self.db = db

    def start(self, transcript_path: Optional[str] = None) -> Session:
        """Start a new session. Ends any active session first."""
        active = self.db.get_active_session()
        if active:
            self.end(active.session_id, summary="Auto-ended by new session")

        return self.db.create_session(transcript_path=transcript_path)

    def end(self, session_id: Optional[int] = None, summary: Optional[str] = None) -> Optional[Session]:
        """End the current or specified session."""
        if session_id is None:
            active = self.db.get_active_session()
            if not active:
                return None
            session_id = active.session_id

        self.db.end_session(session_id, summary=summary)
        return self.db.get_session(session_id)

    def get_active(self) -> Optional[Session]:
        """Get the current active session, if any."""
        return self.db.get_active_session()

    def get_history(self, limit: int = 10) -> list[dict[str, Any]]:
        """Get recent sessions with change counts."""
        rows = self.db._conn.execute(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM change_log cl WHERE cl.session_id = s.session_id) as change_count
               FROM sessions s
               ORDER BY s.session_id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        return [{
            "session_id": r["session_id"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "summary": r["summary"],
            "change_count": r["change_count"],
        } for r in rows]
