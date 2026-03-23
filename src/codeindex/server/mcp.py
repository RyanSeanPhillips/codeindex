"""
MCP stdio server — 8 tools for AI coding agents.

JSON-RPC 2.0 over stdin/stdout, newline-delimited.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from ..config import ProjectConfig
from ..core.indexer import Indexer
from ..core.query import QueryEngine
from ..core.differ import Differ
from ..core.git import GitIntegration
from ..core.snapshot import SnapshotManager
from ..rules.conventions import check_conventions
from ..rules.engine import RuleEngine
from ..sessions.tracker import SessionTracker
from ..sessions.history import SessionHistory
from ..store.db import Database
from ..store.models import Annotation

TOOLS = [
    {
        "name": "index",
        "description": "Build or update the code index. Use mode='full' for complete rebuild, 'incremental' for changed files only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": ["full", "incremental"],
                    "default": "incremental",
                    "description": "Rebuild mode",
                },
            },
        },
    },
    {
        "name": "get_context",
        "description": "Get full context for a symbol: callers, callees, refs, annotations, diagnostics. THE primary tool for understanding code. Returns empty if not indexed — run 'index' first.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name"},
                "kind": {"type": "string", "enum": ["function", "method", "class"], "description": "Filter by kind"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "callers",
        "description": "Who calls this function/method? Returns caller name, class, file, and line number for each call site. Requires index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Function or method name to find callers of"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["name"],
        },
    },
    {
        "name": "get_impact",
        "description": "What breaks if I change this symbol? Shows direct callers, transitive callers, affected files. Requires index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name to analyze impact for"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "search",
        "description": "Full-text + structured search across the codebase. Finds symbols, files, and docstrings. Requires index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
                "kind": {"type": "string", "enum": ["function", "method", "class"], "description": "Filter by symbol kind"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "file_summary",
        "description": "Get structured overview of a file: symbols, imports, diagnostics. Requires index.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path (e.g. 'src/main.py')"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "diagnostics",
        "description": "Run analysis rules, view findings, add/rate rules. Actions: 'run', 'list', 'add_rule', 'rate'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["run", "list", "add_rule", "rate", "effectiveness", "test_rule"],
                    "default": "list",
                    "description": "Action to perform",
                },
                "severity": {"type": "string", "enum": ["error", "warning", "info"]},
                "rule_id": {"type": "string", "description": "Rule ID for rate/run_one"},
                "file_pattern": {"type": "string", "description": "Filter by file path (include)"},
                "exclude_pattern": {"type": "string", "description": "Exclude files matching this pattern"},
                "limit": {"type": "integer", "default": 50},
                "rule_name": {"type": "string", "description": "Name for new rule (add_rule)"},
                "rule_sql": {"type": "string", "description": "SQL query for new rule (add_rule)"},
                "useful": {"type": "boolean", "description": "Rate a rule run as useful (rate)"},
                "weight": {"type": "number", "description": "Importance weight for new rule (add_rule)", "default": 1.0},
                "learned_from": {"type": "string", "description": "Source of this rule (add_rule), e.g. 'CLAUDE.md'"},
            },
        },
    },
    {
        "name": "annotate",
        "description": "Add persistent notes to symbols or files. Notes survive re-indexing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "list"], "default": "add"},
                "symbol_name": {"type": "string", "description": "Symbol to annotate"},
                "file_path": {"type": "string", "description": "File to annotate"},
                "text": {"type": "string", "description": "Note text (for add)"},
                "author": {"type": "string", "default": "ai", "description": "Author: 'user' or 'ai'"},
            },
        },
    },
    {
        "name": "session",
        "description": "Manage coding sessions. Actions: 'start', 'end', 'status', 'changes', 'history'.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "end", "status", "changes", "history"],
                    "default": "status",
                },
                "transcript_path": {"type": "string", "description": "Path to conversation transcript (start)"},
                "summary": {"type": "string", "description": "Session summary (end)"},
            },
        },
    },
    {
        "name": "check_conventions",
        "description": "Check architectural layer boundary violations. Requires layers defined in .codeindex.yaml.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "history",
        "description": "Git-aware history: structural diffs between commits/snapshots, feature change tracking, recent activity.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["diff", "feature_history", "recent_changes", "snapshots"],
                    "default": "recent_changes",
                    "description": "Action to perform",
                },
                "commit": {"type": "string", "description": "Git commit/ref for diff (e.g. 'HEAD~5', 'abc123')"},
                "snapshot": {"type": "string", "description": "Snapshot name/ID for diff"},
                "symbol_name": {"type": "string", "description": "Symbol name for feature_history"},
                "since": {"type": "string", "description": "Time range, e.g. '1 week', '3 days'"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
]


class MCPServer:
    """MCP stdio server for code index tools."""

    def __init__(self, project_root: Path, db_path: Optional[Path] = None):
        self.project_root = project_root.resolve()
        self.config = ProjectConfig.load(self.project_root)
        if db_path is None:
            db_path = self.project_root / ".codeindex.db"
        self.db = Database(db_path)
        self.indexer = Indexer(self.db, self.project_root, config=self.config)
        self.query = QueryEngine(
            self.db,
            project_root=self.project_root,
            inline_source_max_lines=self.config.inline_source_max_lines,
        )
        self.rules = RuleEngine(self.db)
        self.differ = Differ(self.db, self.indexer)
        self.sessions = SessionTracker(self.db)
        self.session_history = SessionHistory(self.db, self.differ)
        self.git = GitIntegration(self.project_root)
        self.snapshots = SnapshotManager(self.db, self.project_root)

        # Seed built-in rules on first run
        self.rules.seed_builtins()

    def handle_tool(self, name: str, args: dict) -> dict:
        try:
            result = self._dispatch(name, args)
            text = json.dumps(result, indent=2, default=str)
            return {"content": [{"type": "text", "text": text}]}
        except Exception as e:
            return {"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True}

    def _dispatch(self, name: str, args: dict) -> Any:
        if name == "index":
            mode = args.get("mode", "incremental")
            if mode == "full":
                stats = self.indexer.full_rebuild()
                diag_results = self.rules.run_all()
                response = {"stats": asdict(stats), "diagnostics_run": diag_results}
            else:
                result = self.indexer.incremental()
                if sum(result.values()) > 0:
                    self.rules.run_all()
                response = result

            # Surface parse error details when errors exist
            error_count = self.db._conn.execute(
                "SELECT COUNT(*) FROM files WHERE parse_error IS NOT NULL"
            ).fetchone()[0]
            if error_count > 0:
                error_files = self.db._conn.execute(
                    "SELECT rel_path, language, parse_error FROM files WHERE parse_error IS NOT NULL LIMIT 5"
                ).fetchall()
                response["parse_error_details"] = [{
                    "file": r["rel_path"], "language": r["language"],
                    "error": r["parse_error"],
                } for r in error_files]
                if error_count > 5:
                    response["parse_error_details"].append(
                        {"note": f"... and {error_count - 5} more"}
                    )
                sym_count = self.db._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
                if sym_count == 0:
                    response["hint"] = (
                        "All files failed to parse. Check that required tree-sitter packages "
                        "are installed: pip install codeindex[all-languages]"
                    )
            return response

        elif name == "get_context":
            warning = self._check_index_populated()
            if warning:
                return warning
            ctx = self.query.get_context(args["name"], kind=args.get("kind"))
            return ctx.to_dict()

        elif name == "callers":
            warning = self._check_index_populated()
            if warning:
                return warning
            return self.query.get_callers(args["name"], limit=args.get("limit", 50))

        elif name == "get_impact":
            warning = self._check_index_populated()
            if warning:
                return warning
            return self.query.get_impact(args["name"])

        elif name == "search":
            warning = self._check_index_populated()
            if warning:
                return warning
            return self.query.search(
                args["query"], kind=args.get("kind"), limit=args.get("limit", 20),
            )

        elif name == "file_summary":
            result = self.query.get_file_summary(args["path"])
            if result is None:
                raise ValueError(f"File not found: {args['path']}")
            return result

        elif name == "diagnostics":
            return self._handle_diagnostics(args)

        elif name == "annotate":
            return self._handle_annotate(args)

        elif name == "session":
            return self._handle_session(args)

        elif name == "check_conventions":
            violations = check_conventions(self.db, self.config)
            return {
                "violations": violations,
                "total": len(violations),
                "has_layers": bool(self.config.layers),
            }

        elif name == "history":
            return self._handle_history(args)

        else:
            raise ValueError(f"Unknown tool: {name}")

    def _handle_diagnostics(self, args: dict) -> Any:
        action = args.get("action", "list")

        if action == "run":
            rule_id = args.get("rule_id")
            if rule_id:
                self.rules.run_one(rule_id)
            else:
                self.rules.run_all()
            # Return filtered diagnostics after running
            return self.db.get_diagnostics(
                severity=args.get("severity"),
                rule_id=args.get("rule_id") if rule_id else None,
                file_pattern=args.get("file_pattern"),
                exclude_pattern=args.get("exclude_pattern"),
                limit=args.get("limit", 50),
            )

        elif action == "list":
            return self.db.get_diagnostics(
                severity=args.get("severity"),
                rule_id=args.get("rule_id"),
                file_pattern=args.get("file_pattern"),
                exclude_pattern=args.get("exclude_pattern"),
                limit=args.get("limit", 50),
            )

        elif action == "add_rule":
            rule_id = args.get("rule_id", "")
            rule_name = args.get("rule_name", "")
            rule_sql = args.get("rule_sql", "")
            if not rule_id or not rule_sql:
                raise ValueError("rule_id and rule_sql required for add_rule")
            rule = self.rules.add_rule(
                rule_id, rule_name, rule_sql,
                severity=args.get("severity", "warning"),
                weight=args.get("weight", 1.0),
                learned_from=args.get("learned_from"),
            )
            return {"rule_id": rule.rule_id, "name": rule.name, "status": "created"}

        elif action == "test_rule":
            rule_sql = args.get("rule_sql", "")
            if not rule_sql:
                raise ValueError("rule_sql required for test_rule")
            rows = self.rules.test_rule(rule_sql)
            return {"preview": rows, "count": len(rows)}

        elif action == "rate":
            rule_id = args.get("rule_id", "")
            useful = args.get("useful", True)
            self.rules.rate_rule(rule_id, useful)
            return {"rule_id": rule_id, "rated": useful}

        elif action == "effectiveness":
            return self.rules.get_effectiveness()

        raise ValueError(f"Unknown diagnostics action: {action}")

    def _handle_annotate(self, args: dict) -> Any:
        action = args.get("action", "add")

        if action == "add":
            text = args.get("text", "")
            if not text:
                raise ValueError("text required for annotation")

            file_id = None
            symbol_id = None

            if args.get("file_path"):
                f = self.db.get_file_by_path(args["file_path"])
                if f:
                    file_id = f.file_id

            if args.get("symbol_name"):
                symbols = self.db.find_symbols(name=args["symbol_name"], limit=1)
                if symbols:
                    symbol_id = symbols[0]["symbol_id"]
                    if not file_id:
                        f = self.db.get_file_by_path(symbols[0]["file"])
                        if f:
                            file_id = f.file_id

            ann = self.db.insert_annotation(Annotation(
                file_id=file_id,
                symbol_id=symbol_id,
                text=text,
                author=args.get("author", "ai"),
            ))
            return {"annotation_id": ann.annotation_id, "status": "created"}

        elif action == "list":
            file_id = None
            symbol_id = None
            if args.get("file_path"):
                f = self.db.get_file_by_path(args["file_path"])
                if f:
                    file_id = f.file_id
            if args.get("symbol_name"):
                symbols = self.db.find_symbols(name=args["symbol_name"], limit=1)
                if symbols:
                    symbol_id = symbols[0]["symbol_id"]
            return self.db.get_annotations(file_id=file_id, symbol_id=symbol_id)

        raise ValueError(f"Unknown annotate action: {action}")

    def _handle_session(self, args: dict) -> Any:
        action = args.get("action", "status")

        if action == "start":
            session = self.sessions.start(transcript_path=args.get("transcript_path"))
            return {
                "session_id": session.session_id,
                "started_at": session.started_at,
                "status": "started",
            }

        elif action == "end":
            session = self.sessions.end(summary=args.get("summary"))
            if session:
                # Record changes
                changes = self.session_history.record_snapshot(session.session_id)
                return {
                    "session_id": session.session_id,
                    "ended_at": session.ended_at,
                    "changes_recorded": len(changes),
                    "status": "ended",
                }
            return {"status": "no_active_session"}

        elif action == "status":
            active = self.sessions.get_active()
            if active:
                changes = self.session_history.current_changes()
                return {
                    "session_id": active.session_id,
                    "started_at": active.started_at,
                    "pending_changes": len(changes),
                    "changes": changes[:20],
                }
            return {"status": "no_active_session"}

        elif action == "changes":
            active = self.sessions.get_active()
            if active:
                return self.session_history.current_changes()
            return []

        elif action == "history":
            return self.sessions.get_history()

        raise ValueError(f"Unknown session action: {action}")

    def _handle_history(self, args: dict) -> Any:
        action = args.get("action", "recent_changes")

        if action == "diff":
            commit = args.get("commit")
            snapshot = args.get("snapshot")
            if commit:
                result = self.snapshots.diff_from_git(commit)
            elif snapshot:
                result = self.snapshots.diff_from_snapshot(snapshot)
            else:
                raise ValueError("Either 'commit' or 'snapshot' required for diff action")
            return result.to_dict()

        elif action == "feature_history":
            symbol_name = args.get("symbol_name")
            if not symbol_name:
                raise ValueError("'symbol_name' required for feature_history action")
            since = args.get("since", "1 week")
            limit = args.get("limit", 20)

            # Get symbol's dependency cone
            ctx = self.query.get_context(symbol_name)
            if not ctx.symbol:
                return {"error": f"Symbol '{symbol_name}' not found in index"}

            # Collect file paths from the dependency cone
            files = set()
            if ctx.symbol.get("file"):
                files.add(ctx.symbol["file"])
            for c in ctx.callers:
                if c.get("file"):
                    files.add(c["file"])
            for c in ctx.callees:
                if c.get("file"):
                    files.add(c["file"])

            # Query git for commits touching those files
            commits = self.git.get_log(since=since, paths=list(files), limit=limit)

            return {
                "symbol": symbol_name,
                "file": ctx.symbol.get("file"),
                "dependency_cone_files": sorted(files),
                "commits": commits,
            }

        elif action == "recent_changes":
            since = args.get("since", "1 week")
            limit = args.get("limit", 20)
            commits = self.git.get_log_with_files(since=since, limit=limit)
            return {
                "git_available": self.git.available,
                "commits": commits,
                "total": len(commits),
            }

        elif action == "snapshots":
            limit = args.get("limit", 20)
            return self.snapshots.list_snapshots(limit=limit)

        raise ValueError(f"Unknown history action: {action}")

    def _check_index_populated(self) -> Optional[dict]:
        """Check if the index has data. Returns warning dict if empty, None if OK."""
        count = self.db._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if count == 0:
            return {
                "warning": "Index is empty. Run the 'index' tool first to build the code index.",
                "hint": "Use: index with mode='full' for first-time indexing.",
            }
        sym_count = self.db._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        if sym_count == 0:
            error_count = self.db._conn.execute(
                "SELECT COUNT(*) FROM files WHERE parse_error IS NOT NULL"
            ).fetchone()[0]
            if error_count > 0:
                error_files = self.db._conn.execute(
                    "SELECT rel_path, parse_error FROM files WHERE parse_error IS NOT NULL LIMIT 5"
                ).fetchall()
                return {
                    "warning": f"Index has {count} files but 0 symbols — all files failed to parse.",
                    "parse_errors": [{
                        "file": r["rel_path"], "error": r["parse_error"],
                    } for r in error_files],
                    "hint": "Check that the required tree-sitter grammar packages are installed. "
                            "E.g.: pip install tree-sitter-typescript tree-sitter-powershell",
                }
        return None

    def run(self):
        """Run the MCP server loop (stdio)."""
        while True:
            msg_id = None
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue

                msg = json.loads(line)
                method = msg.get("method")
                msg_id = msg.get("id")
                params = msg.get("params", {})

                if method == "initialize":
                    self._write({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "protocolVersion": "2024-11-05",
                            "capabilities": {"tools": {}},
                            "serverInfo": {"name": "codeindex", "version": "0.1.0"},
                        },
                    })
                elif method == "notifications/initialized":
                    pass
                elif method == "tools/list":
                    self._write({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"tools": TOOLS},
                    })
                elif method == "tools/call":
                    result = self.handle_tool(params.get("name", ""), params.get("arguments", {}))
                    self._write({"jsonrpc": "2.0", "id": msg_id, "result": result})
                elif msg_id is not None:
                    self._write({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"Unknown method: {method}"},
                    })

            except Exception as e:
                sys.stderr.write(f"MCP Error: {e}\n")
                sys.stderr.flush()
                if msg_id is not None:
                    self._write({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32603, "message": str(e)},
                    })

    def _write(self, msg: dict):
        print(json.dumps(msg), flush=True)
