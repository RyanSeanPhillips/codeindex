"""
Git integration — subprocess wrapper for git commands.

All methods return None or empty list on failure (graceful degradation).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional


class GitIntegration:
    """Git subprocess wrapper. Safe to use when git is unavailable."""

    def __init__(self, project_root: Path):
        self.root = project_root.resolve()
        self.available = self._check_git()

    def _check_git(self) -> bool:
        """Check if this is a git repo with git installed."""
        # Quick filesystem check before shelling out
        if not (self.root / ".git").exists():
            return False
        try:
            result = self._run("rev-parse", "--git-dir", timeout=5)
            return result is not None
        except Exception:
            return False

    def _run(self, *args: str, timeout: int = 30) -> Optional[str]:
        """Run a git command, return stdout or None on failure."""
        try:
            result = subprocess.run(
                ["git"] + list(args),
                capture_output=True, text=True, timeout=timeout,
                cwd=str(self.root), encoding="utf-8", errors="replace",
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return None

    def get_head_commit(self) -> Optional[str]:
        """Get current HEAD commit hash (full 40-char)."""
        if not self.available:
            return None
        return self._run("rev-parse", "HEAD")

    def get_short_hash(self, commit: str = "HEAD") -> Optional[str]:
        """Get short hash for a commit."""
        if not self.available:
            return None
        return self._run("rev-parse", "--short", commit)

    def get_changed_files(self, from_commit: str, to_commit: str = "HEAD") -> list[str]:
        """Get list of files changed between two commits (relative paths, forward slashes)."""
        if not self.available:
            return []
        output = self._run("diff", "--name-only", from_commit, to_commit)
        if not output:
            return []
        return [line for line in output.splitlines() if line.strip()]

    def get_file_at_commit(self, commit: str, rel_path: str) -> Optional[str]:
        """Get file contents at a specific commit. Path must use forward slashes."""
        if not self.available:
            return None
        # Git always uses forward slashes
        git_path = rel_path.replace("\\", "/")
        return self._run("show", f"{commit}:{git_path}", timeout=10)

    # Delimiter for git log format (triple pipe — unlikely to appear in commit messages)
    _SEP = "|||"

    def get_log(
        self,
        since: Optional[str] = None,
        paths: Optional[list[str]] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get git log entries. Returns list of commit dicts."""
        if not self.available:
            return []

        args = ["log", f"--max-count={limit}",
                f"--format=%H{self._SEP}%an{self._SEP}%at{self._SEP}%s"]

        if since:
            args.append(f"--since={since}")

        if paths:
            args.append("--")
            args.extend(p.replace("\\", "/") for p in paths)

        output = self._run(*args)
        if not output:
            return []

        entries = []
        for line in output.splitlines():
            parts = line.split(self._SEP)
            if len(parts) >= 4:
                entries.append({
                    "commit": parts[0],
                    "author": parts[1],
                    "timestamp": parts[2],
                    "subject": parts[3],
                })
        return entries

    def get_log_with_files(
        self,
        since: Optional[str] = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Get git log with changed file lists per commit."""
        if not self.available:
            return []

        args = ["log", f"--max-count={limit}", "--name-only",
                f"--format=%H{self._SEP}%an{self._SEP}%at{self._SEP}%s"]

        if since:
            args.append(f"--since={since}")

        output = self._run(*args)
        if not output:
            return []

        entries = []
        current: Optional[dict] = None
        for line in output.splitlines():
            if self._SEP in line:
                if current:
                    entries.append(current)
                parts = line.split(self._SEP)
                current = {
                    "commit": parts[0],
                    "author": parts[1],
                    "timestamp": parts[2],
                    "subject": parts[3],
                    "files": [],
                }
            elif line.strip() and current:
                current["files"].append(line.strip())

        if current:
            entries.append(current)

        return entries

    def get_line_log(
        self,
        rel_path: str,
        line_start: int,
        line_end: int,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get commits that touched specific lines (git log -L)."""
        if not self.available:
            return []

        git_path = rel_path.replace("\\", "/")
        output = self._run(
            "log", f"--max-count={limit}",
            f"--format=%H{self._SEP}%an{self._SEP}%at{self._SEP}%s",
            f"-L{line_start},{line_end}:{git_path}",
            timeout=30,
        )
        if not output:
            return []

        entries = []
        for line in output.splitlines():
            if self._SEP in line:
                parts = line.split(self._SEP)
                if len(parts) >= 4:
                    entries.append({
                        "commit": parts[0],
                        "author": parts[1],
                        "timestamp": parts[2],
                        "subject": parts[3],
                    })
        return entries
