"""
Query engine — assemble rich context for symbols, files, impact analysis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..store.db import Database


@dataclass
class SymbolContext:
    """Rich context for a symbol — everything an AI agent needs."""
    symbol: dict[str, Any] = field(default_factory=dict)
    callers: list[dict[str, Any]] = field(default_factory=list)
    callees: list[dict[str, Any]] = field(default_factory=list)
    refs: list[dict[str, Any]] = field(default_factory=list)
    annotations: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    siblings: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "callers": self.callers,
            "callees": self.callees,
            "refs": self.refs,
            "annotations": self.annotations,
            "diagnostics": self.diagnostics,
            "siblings": self.siblings,
        }


class QueryEngine:
    """Assemble structured context from the index."""

    def __init__(self, db: Database):
        self.db = db

    def get_context(self, name: str, kind: Optional[str] = None) -> SymbolContext:
        """THE primary tool: get everything about a symbol.

        Returns callers, callees, refs, annotations, diagnostics, siblings.
        """
        # Find the symbol
        symbols = self.db.find_symbols(name=name, kind=kind, limit=1)
        if not symbols:
            # Try exact match
            rows = self.db._conn.execute(
                "SELECT s.*, f.rel_path, p.name as parent_name, p.kind as parent_kind "
                "FROM symbols s JOIN files f ON s.file_id = f.file_id "
                "LEFT JOIN symbols p ON s.parent_id = p.symbol_id "
                "WHERE s.name = ? LIMIT 1",
                (name,),
            ).fetchall()
            if rows:
                symbols = [self.db._symbol_row_to_dict(rows[0])]

        if not symbols:
            return SymbolContext()

        sym = symbols[0]
        sid = sym["symbol_id"]
        ctx = SymbolContext(symbol=sym)

        # Callers — who calls this symbol?
        ctx.callers = self.db.get_callers(name, limit=30)

        # Callees — what does this symbol call?
        ctx.callees = self.db.get_callees(sid)

        # Refs — attribute references within this symbol
        ref_rows = self.db._conn.execute(
            """SELECT r.ref_kind, r.target, r.name, r.line_no, f.rel_path
               FROM refs r JOIN files f ON r.file_id = f.file_id
               WHERE r.symbol_id = ? ORDER BY r.line_no""",
            (sid,),
        ).fetchall()
        ctx.refs = [{
            "ref_kind": r["ref_kind"], "target": r["target"],
            "name": r["name"], "line_no": r["line_no"], "file": r["rel_path"],
        } for r in ref_rows]

        # Annotations
        ctx.annotations = self.db.get_annotations(symbol_id=sid)

        # Diagnostics for this file/line range
        diag_rows = self.db._conn.execute(
            """SELECT d.rule_id, d.severity, d.message, d.line_no
               FROM diagnostics d
               WHERE d.file_id = (SELECT file_id FROM symbols WHERE symbol_id = ?)
               AND d.is_resolved = 0
               AND d.line_no BETWEEN ? AND ?""",
            (sid, sym.get("line_start", 0), sym.get("line_end", 99999)),
        ).fetchall()
        ctx.diagnostics = [{
            "rule_id": d["rule_id"], "severity": d["severity"],
            "message": d["message"], "line_no": d["line_no"],
        } for d in diag_rows]

        # Siblings (other symbols in the same parent)
        parent_name = sym.get("parent_name")
        if parent_name:
            sibling_rows = self.db._conn.execute(
                """SELECT s.name, s.kind, s.line_start, s.line_end
                   FROM symbols s
                   WHERE s.parent_id = (SELECT parent_id FROM symbols WHERE symbol_id = ?)
                   AND s.symbol_id != ?
                   ORDER BY s.line_start LIMIT 20""",
                (sid, sid),
            ).fetchall()
            ctx.siblings = [{
                "name": s["name"], "kind": s["kind"],
                "line_start": s["line_start"], "line_end": s["line_end"],
            } for s in sibling_rows]

        return ctx

    def get_impact(self, name: str) -> dict[str, Any]:
        """What breaks if I change this symbol?

        Returns direct callers, transitive callers (2 hops), and files affected.
        """
        # Direct callers
        direct = self.db.get_callers(name, limit=50)
        direct_names = {c["caller_name"] for c in direct if c["caller_name"]}
        direct_files = {c["file"] for c in direct}

        # Transitive callers (1 more hop)
        transitive = []
        transitive_files = set()
        for caller_name in direct_names:
            if caller_name:
                indirect = self.db.get_callers(caller_name, limit=20)
                for c in indirect:
                    if c["caller_name"] not in direct_names and c["caller_name"] != name:
                        transitive.append(c)
                        transitive_files.add(c["file"])

        all_files = direct_files | transitive_files

        return {
            "symbol": name,
            "direct_callers": direct,
            "transitive_callers": transitive[:30],
            "files_affected": sorted(all_files),
            "impact_score": len(direct) + len(transitive) * 0.5,
        }

    def search(self, query: str, kind: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        """Combined FTS + structured search."""
        results = []

        # FTS search
        fts_results = self.db.search_fts(query, limit=limit)
        for r in fts_results:
            results.append({
                "type": "file",
                "rel_path": r["rel_path"],
                "symbol_names": r["symbol_names"],
                "score": r["score"],
            })

        # Symbol search
        symbols = self.db.find_symbols(name=query, kind=kind, limit=limit)
        for s in symbols:
            results.append({
                "type": "symbol",
                "kind": s["kind"],
                "name": s["name"],
                "parent_name": s["parent_name"],
                "file": s["file"],
                "line_start": s["line_start"],
                "score": 10,  # Exact match bonus
            })

        # Deduplicate and sort by score
        seen = set()
        unique = []
        for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True):
            key = (r.get("name", ""), r.get("rel_path", r.get("file", "")))
            if key not in seen:
                seen.add(key)
                unique.append(r)

        return unique[:limit]

    def get_file_summary(self, rel_path: str) -> Optional[dict[str, Any]]:
        """Structured overview of a file."""
        return self.db.get_file_summary(rel_path)

    def get_imports_graph(self, file_pattern: Optional[str] = None) -> dict[str, Any]:
        """Build an import dependency graph."""
        sql = """
            SELECT f.rel_path, i.module, i.name, i.is_from
            FROM imports i JOIN files f ON i.file_id = f.file_id
        """
        params = []
        if file_pattern:
            sql += " WHERE f.rel_path LIKE ?"
            params.append(f"%{file_pattern}%")
        sql += " ORDER BY f.rel_path"

        rows = self.db._conn.execute(sql, params).fetchall()

        # Build adjacency list
        graph: dict[str, list[str]] = {}
        for r in rows:
            src = r["rel_path"]
            if src not in graph:
                graph[src] = []
            graph[src].append(r["module"])

        return {"nodes": list(graph.keys()), "edges": graph}
