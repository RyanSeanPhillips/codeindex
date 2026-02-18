"""
Seed rules from project instruction files (CLAUDE.md, etc).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from ..store.db import Database
from ..store.models import Rule


def seed_from_instructions(db: Database, project_root: Path) -> int:
    """Look for CLAUDE.md or similar instruction files and extract patterns.

    Currently just stores the file path as knowledge.
    Full pattern extraction (e.g., "never import X at module level") is future work.
    """
    count = 0
    candidates = ["CLAUDE.md", ".claude/CLAUDE.md", "CONTRIBUTING.md"]

    for candidate in candidates:
        path = project_root / candidate
        if path.exists():
            db.set_knowledge(f"instructions_file:{candidate}", {
                "path": str(path),
                "size": path.stat().st_size,
            })
            count += 1

    return count
