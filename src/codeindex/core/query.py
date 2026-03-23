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
    members: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "symbol": self.symbol,
            "callers": self.callers,
            "callees": self.callees,
            "refs": self.refs,
            "annotations": self.annotations,
            "diagnostics": self.diagnostics,
            "siblings": self.siblings,
        }
        if self.members:
            d["members"] = self.members
        return d


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

        # Class-level aggregation
        if sym["kind"] == "class":
            return self._get_class_context(sym)

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

    def _get_class_context(self, class_sym: dict[str, Any]) -> SymbolContext:
        """Aggregated context for a class: all members, external callers, categorized callees."""
        sid = class_sym["symbol_id"]
        class_name = class_sym["name"]
        ctx = SymbolContext(symbol=class_sym)

        # Get all member methods/functions
        member_rows = self.db._conn.execute(
            """SELECT s.*, f.rel_path, p.name as parent_name, p.kind as parent_kind
               FROM symbols s JOIN files f ON s.file_id = f.file_id
               LEFT JOIN symbols p ON s.parent_id = p.symbol_id
               WHERE s.parent_id = ? ORDER BY s.line_start""",
            (sid,),
        ).fetchall()
        members = [self.db._symbol_row_to_dict(r) for r in member_rows]
        member_names = {m["name"] for m in members}

        # Members list with brief info
        ctx.members = [{
            "name": m["name"], "kind": m["kind"],
            "line_start": m["line_start"], "line_end": m["line_end"],
            "params": m.get("params"), "return_type": m.get("return_type"),
            "is_async": m.get("is_async", False),
            "docstring": (m.get("docstring") or "")[:80] or None,
        } for m in members]

        # Aggregate external callers across all members (excluding self-calls)
        all_callers = []
        seen_callers = set()
        for method_name in [class_name] + sorted(member_names):
            callers = self.db.get_callers(method_name, limit=30)
            for c in callers:
                if c.get("caller_class") == class_name:
                    continue
                key = (c.get("caller_name"), c.get("file"), c.get("line_no"))
                if key not in seen_callers:
                    seen_callers.add(key)
                    c["via_member"] = method_name if method_name != class_name else "__init__"
                    all_callers.append(c)
        ctx.callers = all_callers

        # Aggregate all callees from all methods, categorized
        all_callees = []
        for m in members:
            callees = self.db.get_callees(m["symbol_id"])
            for c in callees:
                c["from_method"] = m["name"]
                all_callees.append(c)
        ctx.callees = self._categorize_callees(all_callees)

        # Annotations on the class
        ctx.annotations = self.db.get_annotations(symbol_id=sid)

        # Diagnostics across all members
        file_id_row = self.db._conn.execute(
            "SELECT file_id FROM symbols WHERE symbol_id = ?", (sid,)
        ).fetchone()
        if file_id_row:
            diag_rows = self.db._conn.execute(
                """SELECT d.rule_id, d.severity, d.message, d.line_no
                   FROM diagnostics d WHERE d.file_id = ? AND d.is_resolved = 0
                   AND d.line_no BETWEEN ? AND ?""",
                (file_id_row["file_id"],
                 class_sym.get("line_start", 0), class_sym.get("line_end", 99999)),
            ).fetchall()
            ctx.diagnostics = [{
                "rule_id": d["rule_id"], "severity": d["severity"],
                "message": d["message"], "line_no": d["line_no"],
            } for d in diag_rows]

        # Siblings (other top-level symbols in the same file, not class members)
        sibling_rows = self.db._conn.execute(
            """SELECT s.name, s.kind, s.line_start, s.line_end
               FROM symbols s WHERE s.file_id = ? AND s.parent_id IS NULL
               AND s.symbol_id != ? ORDER BY s.line_start LIMIT 20""",
            (class_sym.get("symbol_id"), sid),
        ).fetchall()
        ctx.siblings = [{
            "name": s["name"], "kind": s["kind"],
            "line_start": s["line_start"], "line_end": s["line_end"],
        } for s in sibling_rows]

        return ctx

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
        """Search for symbols by name or keyword. Returns matching symbols, ranked by relevance.

        Supports single-word exact/prefix/substring matches and multi-word keyword queries.
        Scoring: exact name match > prefix > tokenized name matches > FTS docstring/name matches.
        """
        import re
        results = []
        seen_ids: set[int] = set()

        def _add_result(r: dict[str, Any]) -> None:
            sid = r.get("symbol_id")
            if sid and sid in seen_ids:
                return
            if sid:
                seen_ids.add(sid)
            results.append(r)

        # Tokenize query: split on whitespace, underscores, camelCase boundaries
        tokens = re.split(r'[\s_]+', query.strip())
        tokens = [t.lower() for t in tokens if t]
        is_multi_word = len(tokens) > 1

        # --- Strategy 1: Direct symbol name search (single query string) ---
        symbols = self.db.find_symbols(name=query, kind=kind, limit=limit * 2)
        for s in symbols:
            name = s["name"]
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
            _add_result({
                "type": "symbol", "symbol_id": s["symbol_id"],
                "kind": s["kind"], "name": s["name"],
                "parent_name": s["parent_name"], "file": s["file"],
                "line_start": s["line_start"], "line_end": s.get("line_end"),
                "complexity": s.get("complexity"),
                "docstring": (s.get("docstring") or "")[:100] or None,
                "score": score,
            })

        # --- Strategy 2: Tokenized search (for multi-word queries) ---
        if is_multi_word and len(results) < limit:
            # Search each token individually and score by match count
            token_hits: dict[int, dict] = {}  # symbol_id -> {symbol_data, matched_tokens}
            for token in tokens:
                token_symbols = self.db.find_symbols(name=token, kind=kind, limit=limit * 3)
                for s in token_symbols:
                    sid = s["symbol_id"]
                    if sid not in token_hits:
                        token_hits[sid] = {"symbol": s, "matched": set()}
                    token_hits[sid]["matched"].add(token)

            for sid, info in token_hits.items():
                match_count = len(info["matched"])
                if match_count < 1:
                    continue
                s = info["symbol"]
                score = match_count * 20
                _add_result({
                    "type": "symbol", "symbol_id": s["symbol_id"],
                    "kind": s["kind"], "name": s["name"],
                    "parent_name": s["parent_name"], "file": s["file"],
                    "line_start": s["line_start"], "line_end": s.get("line_end"),
                    "complexity": s.get("complexity"),
                    "docstring": (s.get("docstring") or "")[:100] or None,
                    "score": score,
                })

        # --- Strategy 3: Symbol-level FTS (matches names + docstrings) ---
        if len(results) < limit:
            # Build FTS query: OR-join tokens for broad matching
            fts_query = " OR ".join(tokens) if tokens else query
            fts_results = self.db.search_symbol_fts(fts_query, limit=limit)
            for r in fts_results:
                if kind and r.get("kind") != kind:
                    continue
                _add_result({
                    "type": "symbol", "symbol_id": r["symbol_id"],
                    "kind": r["kind"], "name": r["name"],
                    "parent_name": r.get("parent_name"), "file": r["file"],
                    "line_start": r["line_start"], "line_end": r.get("line_end"),
                    "complexity": r.get("complexity"),
                    "docstring": r.get("docstring"),
                    "score": r["score"] * 0.8,  # Slightly lower than direct symbol matches
                })

        # --- Strategy 4: File-level FTS fallback ---
        if len(results) < limit:
            fts_query = " OR ".join(tokens) if tokens else query
            fts_results = self.db.search_fts(fts_query, limit=limit)
            files_seen = {res.get("file") for res in results if res.get("file")}
            for r in fts_results:
                if r["rel_path"] not in files_seen:
                    results.append({
                        "type": "file",
                        "file": r["rel_path"],
                        "score": r["score"] * 0.3,
                    })

        # Sort by score descending
        results.sort(key=lambda x: x.get("score", 0), reverse=True)
        return results[:limit]

    def get_file_summary(self, rel_path: str) -> Optional[dict[str, Any]]:
        """Structured overview of a file."""
        return self.db.get_file_summary(rel_path)

    def get_overview(self, level: int = 1) -> dict[str, Any]:
        """Project overview at configurable detail levels.

        Level 0 (~200 tokens): project stats + recent git activity.
        Level 1 (~600-1200 tokens): module map with key classes, cross-module deps.
        """
        from collections import defaultdict
        from .git import GitIntegration

        stats = self.db.get_stats()
        git = GitIntegration(self.project_root) if self.project_root else None

        # Languages breakdown
        lang_rows = self.db._conn.execute(
            "SELECT language, COUNT(*) as cnt FROM files GROUP BY language ORDER BY cnt DESC"
        ).fetchall()
        languages = {r["language"]: r["cnt"] for r in lang_rows}

        # Parse error count
        error_count = self.db._conn.execute(
            "SELECT COUNT(*) FROM files WHERE parse_error IS NOT NULL"
        ).fetchone()[0]

        result: dict[str, Any] = {
            "project_root": str(self.project_root) if self.project_root else None,
            "files": stats.total_files,
            "symbols": stats.total_symbols,
            "classes": stats.total_classes,
            "functions": stats.total_functions,
            "languages": languages,
            "parse_errors": error_count,
            "health": {
                "errors": stats.errors,
                "warnings": stats.warnings,
            },
        }

        # Git info
        if git and git.available:
            result["git"] = {
                "head": git.get_short_hash(),
                "recent_commits": git.get_log(limit=5),
            }

            # Hot files (most frequently changed recently)
            log_with_files = git.get_log_with_files(limit=10)
            file_counts: dict[str, int] = {}
            for entry in log_with_files:
                for f in entry.get("files", []):
                    file_counts[f] = file_counts.get(f, 0) + 1
            hot = sorted(file_counts.items(), key=lambda x: -x[1])[:5]
            result["git"]["hot_files"] = [
                {"file": f, "commits": c} for f, c in hot
            ]

        # Testing phase instructions (included in all levels)
        result["codeindex_testing"] = {
            "status": "TESTING PHASE — Help us benchmark codeindex vs standard tools",
            "instructions": [
                "For each structural question (who calls X, what breaks if I change Y, "
                "what does this class depend on), run BOTH approaches:",
                "1. Use codeindex (get_context, get_impact, callers, search)",
                "2. Use grep/read as you normally would",
                "Then compare: token count, round trips, wall-clock time, and whether "
                "codeindex caught dependencies that grep missed.",
            ],
            "features_to_test": [
                "overview (this tool) — does Level 0/1 give useful orientation?",
                "get_context on a class — does it show all members + external callers?",
                "get_impact — does it catch all files affected by a change?",
                "search with multi-word queries — e.g. 'zone classify polygon'",
                "history action=diff commit=HEAD~N — structural diff after changes",
                "history action=feature_history — what commits touched a symbol's deps?",
                "history action=recent_changes — git activity summary",
                "diagnostics with exclude_pattern — filter out backup/test dirs",
            ],
            "metrics_to_report": [
                "tokens: codeindex response size vs grep output size",
                "round_trips: number of tool calls for same answer",
                "time: estimate ~3-5 sec per round trip for inference latency",
                "missed_deps: cases where get_impact found something grep wouldn't",
                "context_noise: irrelevant lines returned by grep that codeindex avoided",
            ],
            "claude_md_overlap": [
                "Track which CLAUDE.md sections you actually referenced during the session.",
                "Note when codeindex's overview/get_context gave you the same info as CLAUDE.md "
                "(architecture, file structure, key classes).",
                "Note what CLAUDE.md told you that codeindex CANNOT provide "
                "(conventions, design rationale, how-to-run, gotchas).",
                "This helps determine what belongs in CLAUDE.md vs what codeindex should handle.",
            ],
            "save_review_to": "reviews/YYYY-MM-DD_<project>_<topic>.md",
        }

        if level == 0:
            return result

        # --- Level 1: Module map with key classes ---
        file_rows = self.db._conn.execute("""
            SELECT f.rel_path, f.line_count,
                   COUNT(DISTINCT s.symbol_id) as symbol_count,
                   SUM(CASE WHEN s.kind = 'class' THEN 1 ELSE 0 END) as classes,
                   SUM(CASE WHEN s.kind IN ('function', 'method') THEN 1 ELSE 0 END) as funcs
            FROM files f
            LEFT JOIN symbols s ON s.file_id = f.file_id
            GROUP BY f.file_id
            ORDER BY f.rel_path
        """).fetchall()

        modules: dict[str, dict] = defaultdict(lambda: {
            "files": 0, "lines": 0, "symbols": 0, "classes": 0, "funcs": 0,
        })
        for r in file_rows:
            path = r["rel_path"]
            parts = path.split("/")
            module = parts[0] if len(parts) > 1 else "(root)"
            m = modules[module]
            m["files"] += 1
            m["lines"] += r["line_count"]
            m["symbols"] += r["symbol_count"] or 0
            m["classes"] += r["classes"] or 0
            m["funcs"] += r["funcs"] or 0

        # Key classes per module (top 5 by method count)
        for mod_name, mod_info in modules.items():
            if mod_info["classes"] == 0:
                continue
            pattern = f"{mod_name}/%" if mod_name != "(root)" else "%"
            class_rows = self.db._conn.execute("""
                SELECT s.name, s.line_start, s.line_end,
                       (SELECT COUNT(*) FROM symbols m
                        WHERE m.parent_id = s.symbol_id) as method_count
                FROM symbols s
                JOIN files f ON s.file_id = f.file_id
                WHERE s.kind = 'class' AND f.rel_path LIKE ?
                ORDER BY method_count DESC LIMIT 5
            """, (pattern,)).fetchall()
            mod_info["key_classes"] = [{
                "name": c["name"],
                "methods": c["method_count"],
                "lines": c["line_end"] - c["line_start"],
            } for c in class_rows]

        # Cross-module dependencies from imports
        import_rows = self.db._conn.execute("""
            SELECT f.rel_path, i.module
            FROM imports i JOIN files f ON i.file_id = f.file_id
            WHERE i.is_from = 1 AND i.module IS NOT NULL
        """).fetchall()

        module_deps: dict[str, set] = defaultdict(set)
        module_names = set(modules.keys())
        for r in import_rows:
            src_parts = r["rel_path"].split("/")
            src_module = src_parts[0] if len(src_parts) > 1 else "(root)"
            imp = r["module"] or ""
            for mod_name in module_names:
                if mod_name != "(root)" and (mod_name in imp or imp.startswith(mod_name)):
                    if mod_name != src_module:
                        module_deps[src_module].add(mod_name)

        result["modules"] = {}
        for mod_name in sorted(modules.keys()):
            m = modules[mod_name]
            mod_dict: dict[str, Any] = {
                "files": m["files"],
                "lines": m["lines"],
                "classes": m["classes"],
                "functions": m["funcs"],
            }
            if m.get("key_classes"):
                mod_dict["key_classes"] = m["key_classes"]
            deps = module_deps.get(mod_name)
            if deps:
                mod_dict["depends_on"] = sorted(deps)
            result["modules"][mod_name] = mod_dict

        return result

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
