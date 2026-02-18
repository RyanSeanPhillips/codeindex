"""
Indexer — parse files, store structured data.

Handles full rebuild, incremental updates, and single-file reindex.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pathspec

from ..parsers.base import ParseResult
from ..parsers.registry import get_parser, supported_extensions
from ..store.db import Database
from ..store.models import File, IndexStats


def compute_file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Default directories to always skip
ALWAYS_SKIP = {
    "__pycache__", ".git", ".hg", ".svn",
    "node_modules", ".venv", "venv", "env",
    "build", "dist", ".eggs", ".mypy_cache", ".pytest_cache",
    ".tox", ".nox",
}


class Indexer:
    """Build and update the code index."""

    def __init__(self, db: Database, project_root: Path, config: Optional[dict] = None):
        self.db = db
        self.project_root = project_root.resolve()
        self.config = config or {}
        self._ignore_spec = self._build_ignore_spec()

    def _build_ignore_spec(self) -> Optional[pathspec.PathSpec]:
        """Build a pathspec from .gitignore + config ignore patterns."""
        patterns = list(self.config.get("ignore", []))

        gitignore = self.project_root / ".gitignore"
        if gitignore.exists():
            try:
                patterns.extend(gitignore.read_text(errors="replace").splitlines())
            except Exception:
                pass

        if patterns:
            return pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        return None

    def discover_files(self) -> list[tuple[Path, str]]:
        """Walk project, return (abs_path, rel_path) for parseable files."""
        exts = supported_extensions()
        results = []

        for dirpath, dirnames, filenames in os.walk(self.project_root):
            # Skip always-excluded dirs
            dirnames[:] = [
                d for d in dirnames
                if d not in ALWAYS_SKIP and not d.endswith(".egg-info")
            ]

            rel_dir = Path(dirpath).relative_to(self.project_root).as_posix()

            # Skip ignored dirs
            if self._ignore_spec and rel_dir != ".":
                dirnames[:] = [
                    d for d in dirnames
                    if not self._ignore_spec.match_file(f"{rel_dir}/{d}/")
                ]

            for fname in filenames:
                ext = Path(fname).suffix.lower()
                if ext not in exts:
                    continue
                abs_path = Path(dirpath) / fname
                rel_path = abs_path.relative_to(self.project_root).as_posix()

                if self._ignore_spec and self._ignore_spec.match_file(rel_path):
                    continue

                results.append((abs_path, rel_path))

        return results

    def full_rebuild(self, run_diagnostics: bool = True) -> IndexStats:
        """Full rebuild: clear everything, re-parse all files."""
        t0 = time.time()

        # Clear all data
        self.db._conn.executescript("""
            DELETE FROM fts;
            DELETE FROM diagnostics;
            DELETE FROM files;
        """)

        files = self.discover_files()
        with self.db.transaction():
            for abs_path, rel_path in files:
                self._index_file(abs_path, rel_path)

        elapsed = time.time() - t0
        stats = self.db.get_stats()

        self.db.set_knowledge("last_rebuild", {
            "timestamp": datetime.now().isoformat(),
            "elapsed_seconds": round(elapsed, 2),
            "files_indexed": stats.total_files,
        })

        return stats

    def incremental(self) -> dict[str, int]:
        """Re-index only changed/new/deleted files."""
        files = self.discover_files()
        current_paths = {rel for _, rel in files}
        changed = added = removed = 0

        # Remove deleted files
        for existing in self.db.list_files():
            if existing.rel_path not in current_paths:
                self.db.delete_file(existing.file_id)
                removed += 1

        # Index new/changed files
        with self.db.transaction():
            for abs_path, rel_path in files:
                existing = self.db.get_file_by_path(rel_path)
                file_hash = compute_file_hash(abs_path)

                if existing is None:
                    self._index_file(abs_path, rel_path)
                    added += 1
                elif existing.file_hash != file_hash:
                    self.db.delete_file(existing.file_id)
                    self._index_file(abs_path, rel_path)
                    changed += 1

        return {"changed": changed, "added": added, "removed": removed}

    def reindex_file(self, rel_path: str) -> bool:
        """Re-index a single file."""
        abs_path = self.project_root / rel_path
        if not abs_path.exists():
            existing = self.db.get_file_by_path(rel_path)
            if existing:
                self.db.delete_file(existing.file_id)
            return True

        existing = self.db.get_file_by_path(rel_path)
        if existing:
            self.db.delete_file(existing.file_id)

        with self.db.transaction():
            self._index_file(abs_path, rel_path)
        return True

    def _index_file(self, abs_path: Path, rel_path: str):
        """Parse and store a single file."""
        parser = get_parser(abs_path)
        if not parser:
            return

        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            self._store_file_error(abs_path, rel_path, str(e))
            return

        file_hash = compute_file_hash(abs_path)
        line_count = source.count("\n") + 1

        result = parser.parse(source, rel_path)

        fdef = self.db.upsert_file(File(
            rel_path=rel_path,
            file_hash=file_hash,
            language=parser.language,
            line_count=line_count,
            parse_error=result.parse_error,
            indexed_at=datetime.now().isoformat(),
        ))
        file_id = fdef.file_id

        # Build symbol_id map for linking
        symbol_map: dict[int, int] = {}  # id(Symbol) -> symbol_id

        # Insert symbols (classes first, then functions/methods)
        classes = [s for s in result.symbols if s.kind == "class"]
        others = [s for s in result.symbols if s.kind != "class"]

        for sym in classes:
            sym.file_id = file_id
            parent = getattr(sym, "_pending_parent", None)
            if parent is not None:
                sym.parent_id = symbol_map.get(id(parent))
            self.db.insert_symbol(sym)
            symbol_map[id(sym)] = sym.symbol_id

        for sym in others:
            sym.file_id = file_id
            parent = getattr(sym, "_pending_parent", None)
            if parent is not None:
                sym.parent_id = symbol_map.get(id(parent))
            self.db.insert_symbol(sym)
            symbol_map[id(sym)] = sym.symbol_id

        # Link calls to caller symbols
        for call in result.calls:
            caller = getattr(call, "_pending_caller", None)
            if caller is not None:
                call.caller_id = symbol_map.get(id(caller))
        if result.calls:
            self.db.bulk_insert_calls(file_id, result.calls)

        # Link refs to symbols
        for ref in result.refs:
            sym = getattr(ref, "_pending_symbol", None)
            if sym is not None:
                ref.symbol_id = symbol_map.get(id(sym))
        if result.refs:
            self.db.bulk_insert_refs(file_id, result.refs)

        if result.imports:
            self.db.bulk_insert_imports(file_id, result.imports)

        # Update FTS
        symbol_names = " ".join(s.name for s in result.symbols)
        docstrings = " ".join(s.docstring or "" for s in result.symbols)
        self.db.update_fts(rel_path, symbol_names, docstrings.strip())

    def _store_file_error(self, abs_path: Path, rel_path: str, error: str):
        self.db.upsert_file(File(
            rel_path=rel_path,
            file_hash="",
            line_count=0,
            parse_error=error,
            indexed_at=datetime.now().isoformat(),
        ))
