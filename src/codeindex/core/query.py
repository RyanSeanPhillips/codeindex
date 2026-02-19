"""
Query engine — assemble rich context for symbols, files, impact analysis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from pathlib import Path

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

    def __init__(self, db: Database, project_root: Optional[Path] = None,
                 inline_source_max_lines: int = 0):
        self.db = db
        self.project_root = project_root
        self.inline_source_max_lines = inline_source_max_lines

    def get_context(self, name: str, kind: Optional[str] = None) -> SymbolContext:
        """THE primary tool: get everything about a symbol.

        Returns callers, callees, refs, annotations, diagnostics, siblings.
        """
        # Find the symbol — exact match first, then fuzzy fallback
        sql = (
            "SELECT s.*, f.rel_path, p.name as parent_name, p.kind as parent_kind "
            "FROM symbols s JOIN files f ON s.file_id = f.file_id "
            "LEFT JOIN symbols p ON s.parent_id = p.symbol_id "
            "WHERE s.name = ?"
        )
        params: list = [name]
        if kind:
            sql += " AND s.kind = ?"
            params.append(kind)
        sql += " LIMIT 5"
        rows = self.db._conn.execute(sql, params).fetchall()
        if rows:
            symbols = [self.db._symbol_row_to_dict(r) for r in rows]
        else:
            # Fuzzy fallback only if exact match fails
            symbols = self.db.find_symbols(name=name, kind=kind, limit=5)

        if not symbols:
            return SymbolContext()

        sym = symbols[0]
        sid = sym["symbol_id"]
        ctx = SymbolContext(symbol=sym)

        # Callers — who calls this symbol?
        ctx.callers = self.db.get_callers(name, limit=30)

        # Callees — what does this symbol call? (categorized)
        raw_callees = self.db.get_callees(sid)
        ctx.callees = self._categorize_callees(raw_callees)

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

        # Inline source for small symbols
        if self.inline_source_max_lines > 0:
            line_count = sym.get("line_end", 0) - sym.get("line_start", 0) + 1
            if line_count <= self.inline_source_max_lines:
                source = self._read_source(sym.get("file", ""), sym.get("line_start", 0), sym.get("line_end", 0))
                if source:
                    ctx.symbol["source"] = source

        return ctx

    def get_callers(self, name: str, limit: int = 50) -> list[dict[str, Any]]:
        """Direct wrapper for callers query — exposed as standalone tool."""
        return self.db.get_callers(name, limit=limit)

    def _categorize_callees(self, callees: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Categorize callees into groups: state, self_method, external, stdlib/builtin."""
        # Known Python builtins and common stdlib
        _BUILTINS = {
            "print", "len", "range", "int", "str", "float", "bool", "list", "dict",
            "tuple", "set", "type", "isinstance", "issubclass", "hasattr", "getattr",
            "setattr", "super", "enumerate", "zip", "map", "filter", "sorted", "reversed",
            "min", "max", "sum", "abs", "round", "any", "all", "next", "iter",
            "open", "repr", "id", "hash", "callable", "vars", "dir",
        }

        categorized = []
        for c in callees:
            expr = c.get("callee_expr", "")
            parts = expr.split(".")

            if parts[0] == "self":
                if len(parts) == 2:
                    category = "self_method"
                elif len(parts) >= 3:
                    category = "self_attr_method"
                else:
                    category = "self_method"
            elif parts[-1] in _BUILTINS or (len(parts) == 1 and parts[0] in _BUILTINS):
                category = "builtin"
            elif "." in expr and not expr.startswith("self"):
                category = "external"
            else:
                # Check if it's a known symbol in the index
                category = "local"

            c["category"] = category
            categorized.append(c)

        return categorized

    def _read_source(self, rel_path: str, line_start: int, line_end: int) -> Optional[str]:
        """Read source lines from a file. Returns None if file can't be read."""
        if not self.project_root or not rel_path:
            return None
        try:
            full_path = self.project_root / rel_path
            lines = full_path.read_text(encoding="utf-8").splitlines()
            # line numbers are 1-indexed
            return "\n".join(lines[line_start - 1:line_end])
        except Exception:
            return None

    def get_impact(self, name: str) -> dict[str, Any]:
        """What breaks if I change this symbol?

        Returns direct callers, transitive callers (2 hops), and files affected.
        For classes, aggregates callers across all member methods.
        """
        # Check if this is a class — if so, aggregate callers of all members
        class_row = self.db._conn.execute(
            "SELECT symbol_id FROM symbols WHERE name = ? AND kind = 'class' LIMIT 1",
            (name,),
        ).fetchone()

        if class_row:
            return self._get_class_impact(name, class_row["symbol_id"])

        # Direct callers for a single symbol
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

    def _get_class_impact(self, class_name: str, class_id: int) -> dict[str, Any]:
        """Aggregate callers across all members of a class, excluding self-calls."""
        # Get all member method names
        member_rows = self.db._conn.execute(
            "SELECT name FROM symbols WHERE parent_id = ? AND kind IN ('method', 'function')",
            (class_id,),
        ).fetchall()
        member_names = {r["name"] for r in member_rows}

        # Also get callers of the class itself (constructor calls)
        all_direct = []
        seen_callers = set()  # (caller_name, file, line_no) to deduplicate

        for method_name in [class_name] + sorted(member_names):
            callers = self.db.get_callers(method_name, limit=50)
            for c in callers:
                # Skip self-calls (methods within the same class calling each other)
                if c.get("caller_class") == class_name:
                    continue
                key = (c.get("caller_name"), c.get("file"), c.get("line_no"))
                if key not in seen_callers:
                    seen_callers.add(key)
                    c["via_member"] = method_name if method_name != class_name else "__init__"
                    all_direct.append(c)

        direct_names = {c["caller_name"] for c in all_direct if c["caller_name"]}
        direct_files = {c["file"] for c in all_direct if c.get("file")}

        # Transitive callers (1 more hop)
        transitive = []
        transitive_files = set()
        for caller_name in direct_names:
            if caller_name:
                indirect = self.db.get_callers(caller_name, limit=20)
                for c in indirect:
                    if c["caller_name"] not in direct_names and c["caller_name"] not in member_names:
                        key = (c.get("caller_name"), c.get("file"), c.get("line_no"))
                        if key not in seen_callers:
                            seen_callers.add(key)
                            transitive.append(c)
                            if c.get("file"):
                                transitive_files.add(c["file"])

        all_files = direct_files | transitive_files

        # Group direct callers by member for structured output
        by_member: dict[str, list] = {}
        for c in all_direct:
            member = c.pop("via_member", "unknown")
            by_member.setdefault(member, []).append(c)

        return {
            "symbol": class_name,
            "kind": "class",
            "members_analyzed": sorted(member_names),
            "direct_callers": all_direct,
            "direct_callers_by_member": by_member,
            "transitive_callers": transitive[:30],
            "files_affected": sorted(all_files),
            "impact_score": len(all_direct) + len(transitive) * 0.5,
        }

    def search(self, query: str, kind: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        """Search for symbols by name. Returns only matching symbols, ranked by relevance.

        Scoring: exact name match > prefix match > substring match > FTS file match.
        """
        results = []

        # Symbol search (primary — this is what agents actually want)
        symbols = self.db.find_symbols(name=query, kind=kind, limit=limit * 2)
        for s in symbols:
            name = s["name"]
            # Score: exact > prefix > substring
            if name == query:
                score = 100
            elif name.startswith(query):
                score = 50
            elif name.lower() == query.lower():
                score = 90
            elif name.lower().startswith(query.lower()):
                score = 40
            else:
                score = 10
            results.append({
                "type": "symbol",
                "kind": s["kind"],
                "name": s["name"],
                "parent_name": s["parent_name"],
                "file": s["file"],
                "line_start": s["line_start"],
                "line_end": s.get("line_end"),
                "complexity": s.get("complexity"),
                "docstring": (s.get("docstring") or "")[:100] or None,
                "score": score,
            })

        # FTS search — only if we have few symbol matches, and only extract
        # individual symbol names that match (not the whole file)
        if len(results) < limit:
            fts_results = self.db.search_fts(query, limit=limit)
            for r in fts_results:
                # Don't add FTS results for files we already have symbols from
                files_seen = {res["file"] for res in results if res.get("file")}
                if r["rel_path"] not in files_seen:
                    results.append({
                        "type": "file",
                        "file": r["rel_path"],
                        "score": r["score"] * 0.5,  # Lower priority than symbol matches
                    })

        # Deduplicate and sort by score
        seen = set()
        unique = []
        for r in sorted(results, key=lambda x: x.get("score", 0), reverse=True):
            key = (r.get("name", ""), r.get("file", r.get("rel_path", "")))
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
