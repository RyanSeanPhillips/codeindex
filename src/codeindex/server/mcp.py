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

from ..core.indexer import Indexer
from ..core.query import QueryEngine
from ..core.differ import Differ
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
        "description": "Get full context for a symbol: callers, callees, refs, annotations, diagnostics. THE primary tool for understanding code.",
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
        "name": "get_impact",
        "description": "What breaks if I change this symbol? Shows direct callers, transitive callers, affected files.",
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
        "description": "Full-text + structured search across the codebase. Finds symbols, files, and docstrings.",
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
        "description": "Get structured overview of a file: symbols, imports, diagnostics.",
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
                    "enum": ["run", "list", "add_rule", "rate", "effectiveness"],
                    "default": "list",
                    "description": "Action to perform",
                },
                "severity": {"type": "string", "enum": ["error", "warning", "info"]},
                "rule_id": {"type": "string", "description": "Rule ID for rate/run_one"},
                "file_pattern": {"type": "string", "description": "Filter by file path"},
                "limit": {"type": "integer", "default": 50},
                "rule_name": {"type": "string", "description": "Name for new rule (add_rule)"},
                "rule_sql": {"type": "string", "description": "SQL query for new rule (add_rule)"},
                "useful": {"type": "boolean", "description": "Rate a rule run as useful (rate)"},
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
]


class MCPServer:
    """MCP stdio server for code index tools."""

    def __init__(self, project_root: Path, db_path: Optional[Path] = None):
        self.project_root = project_root.resolve()
        if db_path is None:
            db_path = self.project_root / ".codeindex.db"
        self.db = Database(db_path)
        self.indexer = Indexer(self.db, self.project_root)
        self.query = QueryEngine(self.db)
        self.rules = RuleEngine(self.db)
        self.differ = Differ(self.db, self.indexer)
        self.sessions = SessionTracker(self.db)
        self.history = SessionHistory(self.db, self.differ)

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
                # Run diagnostics after rebuild
                diag_results = self.rules.run_all()
                return {"stats": asdict(stats), "diagnostics_run": diag_results}
            else:
                result = self.indexer.incremental()
                if sum(result.values()) > 0:
                    self.rules.run_all()
                return result

        elif name == "get_context":
            ctx = self.query.get_context(args["name"], kind=args.get("kind"))
            return ctx.to_dict()

        elif name == "get_impact":
            return self.query.get_impact(args["name"])

        elif name == "search":
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

        else:
            raise ValueError(f"Unknown tool: {name}")

    def _handle_diagnostics(self, args: dict) -> Any:
        action = args.get("action", "list")

        if action == "run":
            rule_id = args.get("rule_id")
            if rule_id:
                count = self.rules.run_one(rule_id)
                return {"rule_id": rule_id, "findings_count": count}
            return self.rules.run_all()

        elif action == "list":
            return self.db.get_diagnostics(
                severity=args.get("severity"),
                rule_id=args.get("rule_id"),
                file_pattern=args.get("file_pattern"),
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
            )
            return {"rule_id": rule.rule_id, "name": rule.name, "status": "created"}

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
                changes = self.history.record_snapshot(session.session_id)
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
                changes = self.history.current_changes()
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
                return self.history.current_changes()
            return []

        elif action == "history":
            return self.sessions.get_history()

        raise ValueError(f"Unknown session action: {action}")

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
