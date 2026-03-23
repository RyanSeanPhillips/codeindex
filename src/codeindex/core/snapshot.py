"""
Snapshot manager — structural snapshots and diff computation.

Captures the call graph state at a point in time and compares it against
the current index to find broken call paths after refactoring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..parsers.registry import get_parser
from ..store.db import Database
from .git import GitIntegration


@dataclass
class StructuralDiff:
    """Result of comparing two index states."""
    symbols_added: list[dict[str, Any]] = field(default_factory=list)
    symbols_removed: list[dict[str, Any]] = field(default_factory=list)
    symbols_modified: list[dict[str, Any]] = field(default_factory=list)
    calls_broken: list[dict[str, Any]] = field(default_factory=list)
    calls_added: list[dict[str, Any]] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbols_added": self.symbols_added,
            "symbols_removed": self.symbols_removed,
            "symbols_modified": self.symbols_modified,
            "calls_broken": self.calls_broken,
            "calls_added": self.calls_added,
            "files_changed": self.files_changed,
            "summary": self.summary,
        }


class SnapshotManager:
    """Manages structural snapshots and computes diffs."""

    def __init__(self, db: Database, project_root: Optional[Path] = None):
        self.db = db
        self.project_root = project_root
        self.git = GitIntegration(project_root) if project_root else None

    def create_snapshot(self, name: Optional[str] = None) -> int:
        """Capture current symbols + calls state. Auto-names with git commit."""
        symbols = self._current_symbols()
        calls = self._current_calls()

        if not name:
            if self.git and self.git.available:
                short = self.git.get_short_hash() or "unknown"
                name = f"git_{short}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            else:
                name = f"snap_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        git_commit = None
        if self.git and self.git.available:
            git_commit = self.git.get_head_commit()

        snapshot_id = self.db.insert_snapshot(
            name=name,
            git_commit=git_commit,
            symbols_json=json.dumps(symbols, separators=(",", ":")),
            calls_json=json.dumps(calls, separators=(",", ":")),
        )

        # Auto-prune
        self.db.delete_old_snapshots(keep=20)

        return snapshot_id

    def list_snapshots(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.db.list_snapshots(limit=limit)

    def prune_snapshots(self, keep: int = 20) -> int:
        return self.db.delete_old_snapshots(keep=keep)

    def diff_from_snapshot(self, name_or_id) -> StructuralDiff:
        """Compare a stored snapshot against the current index."""
        snap = self.db.get_snapshot(name_or_id)
        if not snap:
            diff = StructuralDiff()
            diff.summary = {"error": f"Snapshot '{name_or_id}' not found"}
            return diff

        old_symbols = json.loads(snap["symbols_json"])
        old_calls = json.loads(snap["calls_json"])
        new_symbols = self._current_symbols()
        new_calls = self._current_calls()

        return self._compute_diff(old_symbols, old_calls, new_symbols, new_calls)

    def diff_from_git(self, commit: str) -> StructuralDiff:
        """Rebuild old state from git and compare against current index."""
        if not self.git or not self.git.available:
            diff = StructuralDiff()
            diff.summary = {"error": "Git not available"}
            return diff

        # Check if we have a snapshot for this commit
        snap = self.db.get_snapshot(commit)
        if snap:
            return self.diff_from_snapshot(snap["snapshot_id"])

        # Get changed files between commit and HEAD
        changed_files = self.git.get_changed_files(commit)
        if not changed_files:
            diff = StructuralDiff()
            diff.summary = {"message": "No files changed", "files_changed": 0}
            return diff

        if len(changed_files) > 500:
            diff = StructuralDiff()
            diff.summary = {
                "error": f"Too many changed files ({len(changed_files)}). Use a more recent commit."
            }
            return diff

        # Build old state: parse old versions of changed files + current state of unchanged files
        old_symbols, old_calls = self._build_old_state(commit, changed_files)
        new_symbols = self._current_symbols()
        new_calls = self._current_calls()

        result = self._compute_diff(old_symbols, old_calls, new_symbols, new_calls)
        result.files_changed = sorted(changed_files)
        return result

    def _build_old_state(
        self, commit: str, changed_files: list[str]
    ) -> tuple[list[dict], list[dict]]:
        """Build the old index state by parsing old file versions from git."""
        changed_set = set(changed_files)

        # Start with current symbols/calls from unchanged files
        old_symbols = []
        old_calls = []

        current_symbols = self._current_symbols()
        current_calls = self._current_calls()

        for sym in current_symbols:
            if sym.get("f") not in changed_set:
                old_symbols.append(sym)

        for call in current_calls:
            if call.get("f") not in changed_set:
                old_calls.append(call)

        # Parse old versions of changed files
        for rel_path in changed_files:
            old_source = self.git.get_file_at_commit(commit, rel_path)
            if old_source is None:
                continue  # File didn't exist at that commit (was added)

            parser = get_parser(rel_path)
            if not parser:
                continue

            try:
                result = parser.parse(old_source, rel_path)
            except Exception:
                continue

            for sym in result.symbols:
                parent = getattr(sym, "_pending_parent", None)
                parent_name = parent.name if parent else None
                old_symbols.append({
                    "n": sym.name,
                    "k": sym.kind,
                    "f": rel_path,
                    "ls": sym.line_start,
                    "le": sym.line_end,
                    "p": parent_name,
                })

            for call in result.calls:
                caller = getattr(call, "_pending_caller", None)
                caller_name = caller.name if caller else None
                old_calls.append({
                    "cr": caller_name,
                    "ce": call.callee_expr,
                    "f": rel_path,
                    "l": call.line_no,
                })

        return old_symbols, old_calls

    def _current_symbols(self) -> list[dict]:
        """Get compact symbol list from current index."""
        rows = self.db._conn.execute(
            """SELECT s.name, s.kind, f.rel_path, s.line_start, s.line_end,
                      p.name as parent_name
               FROM symbols s
               JOIN files f ON s.file_id = f.file_id
               LEFT JOIN symbols p ON s.parent_id = p.symbol_id
               ORDER BY f.rel_path, s.line_start"""
        ).fetchall()
        return [{
            "n": r["name"], "k": r["kind"], "f": r["rel_path"],
            "ls": r["line_start"], "le": r["line_end"],
            "p": r["parent_name"],
        } for r in rows]

    def _current_calls(self) -> list[dict]:
        """Get compact call list from current index."""
        rows = self.db._conn.execute(
            """SELECT c.callee_expr, c.line_no, f.rel_path,
                      s.name as caller_name
               FROM calls c
               JOIN files f ON c.file_id = f.file_id
               LEFT JOIN symbols s ON c.caller_id = s.symbol_id
               ORDER BY f.rel_path, c.line_no"""
        ).fetchall()
        return [{
            "cr": r["caller_name"], "ce": r["callee_expr"],
            "f": r["rel_path"], "l": r["line_no"],
        } for r in rows]

    def _compute_diff(
        self,
        old_symbols: list[dict],
        old_calls: list[dict],
        new_symbols: list[dict],
        new_calls: list[dict],
    ) -> StructuralDiff:
        """Compare two index states and find structural changes."""
        diff = StructuralDiff()

        # Build symbol lookup by qualified name
        def _sym_key(s: dict) -> str:
            parent = s.get("p") or ""
            return f"{parent}.{s['n']}" if parent else s["n"]

        old_sym_map = {}
        for s in old_symbols:
            key = _sym_key(s)
            old_sym_map[key] = s

        new_sym_map = {}
        for s in new_symbols:
            key = _sym_key(s)
            new_sym_map[key] = s

        old_keys = set(old_sym_map.keys())
        new_keys = set(new_sym_map.keys())

        # Symbols added/removed
        for key in sorted(new_keys - old_keys):
            s = new_sym_map[key]
            diff.symbols_added.append({
                "name": s["n"], "kind": s["k"], "file": s["f"],
                "line_start": s["ls"],
            })

        for key in sorted(old_keys - new_keys):
            s = old_sym_map[key]
            diff.symbols_removed.append({
                "name": s["n"], "kind": s["k"], "file": s["f"],
                "line_start": s["ls"],
            })

        # Symbols modified (same name, different file or significant line change)
        for key in sorted(old_keys & new_keys):
            old = old_sym_map[key]
            new = new_sym_map[key]
            if old["f"] != new["f"] or old["k"] != new["k"]:
                diff.symbols_modified.append({
                    "name": new["n"],
                    "old_file": old["f"], "old_line": old["ls"],
                    "new_file": new["f"], "new_line": new["ls"],
                    "kind_changed": old["k"] != new["k"],
                })

        # Build call edge sets: (caller_name, callee_expr)
        def _call_key(c: dict) -> tuple:
            return (c.get("cr") or "", c.get("ce") or "")

        old_call_set = {_call_key(c) for c in old_calls}
        new_call_set = {_call_key(c) for c in new_calls}

        # Build lookup for details
        old_call_map = {}
        for c in old_calls:
            key = _call_key(c)
            if key not in old_call_map:
                old_call_map[key] = c

        new_call_map = {}
        for c in new_calls:
            key = _call_key(c)
            if key not in new_call_map:
                new_call_map[key] = c

        # Broken calls: existed before, no equivalent now
        # Only report if the caller still exists (otherwise it's just a removed symbol)
        for key in sorted(old_call_set - new_call_set):
            caller_name, callee_expr = key
            if not caller_name:
                continue
            # Check if caller still exists in new code
            caller_exists = any(
                s["n"] == caller_name for s in new_symbols
            )
            if not caller_exists:
                continue  # Caller was removed entirely — not a broken path

            c = old_call_map[key]
            # Check if callee still exists as a symbol
            callee_short = callee_expr.split(".")[-1] if "." in callee_expr else callee_expr
            callee_exists = any(s["n"] == callee_short for s in new_symbols)

            diff.calls_broken.append({
                "caller": caller_name,
                "callee": callee_expr,
                "file": c["f"],
                "line": c["l"],
                "callee_still_exists": callee_exists,
            })

        # New calls
        for key in sorted(new_call_set - old_call_set):
            caller_name, callee_expr = key
            if not caller_name:
                continue
            c = new_call_map[key]
            diff.calls_added.append({
                "caller": caller_name,
                "callee": callee_expr,
                "file": c["f"],
                "line": c["l"],
            })

        # Files changed (from symbols that differ)
        changed_files = set()
        for s in diff.symbols_added + diff.symbols_removed:
            changed_files.add(s["file"])
        for s in diff.symbols_modified:
            changed_files.add(s.get("old_file", ""))
            changed_files.add(s.get("new_file", ""))
        diff.files_changed = sorted(f for f in changed_files if f)

        # Summary
        diff.summary = {
            "symbols_added": len(diff.symbols_added),
            "symbols_removed": len(diff.symbols_removed),
            "symbols_modified": len(diff.symbols_modified),
            "calls_broken": len(diff.calls_broken),
            "calls_added": len(diff.calls_added),
            "files_changed": len(diff.files_changed),
        }

        return diff
