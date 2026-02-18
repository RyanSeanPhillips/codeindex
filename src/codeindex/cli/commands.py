"""
CLI commands — argparse subcommands for codeindex.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from ..core.indexer import Indexer
from ..core.query import QueryEngine
from ..core.differ import Differ
from ..rules.engine import RuleEngine
from ..rules.seed import seed_from_instructions
from ..sessions.tracker import SessionTracker
from ..sessions.history import SessionHistory
from ..store.db import Database
from . import formatter


def _get_db(args) -> tuple[Database, Path]:
    """Get database and project root from args."""
    project_root = Path(args.project).resolve()
    db_path = project_root / ".codeindex.db"
    db = Database(db_path)
    return db, project_root


def cmd_init(args):
    """Build the full index."""
    db, root = _get_db(args)
    indexer = Indexer(db, root)
    rules = RuleEngine(db)
    rules.seed_builtins()
    seed_from_instructions(db, root)

    print(f"Indexing {root}...", flush=True)
    stats = indexer.full_rebuild()
    diag_results = rules.run_all()

    print(formatter.format_stats(stats))
    total_findings = sum(r["findings_count"] for r in diag_results)
    if total_findings:
        print(f"\n{total_findings} diagnostic findings from {len(diag_results)} rules")
    db.close()


def cmd_update(args):
    """Incremental index update."""
    db, root = _get_db(args)
    indexer = Indexer(db, root)
    result = indexer.incremental()

    total = sum(result.values())
    if total == 0:
        print("Index is up to date.")
    else:
        print(f"Updated: {result['added']} added, {result['changed']} changed, {result['removed']} removed")

        if total > 0:
            rules = RuleEngine(db)
            rules.run_all()
    db.close()


def cmd_context(args):
    """Get context for a symbol."""
    db, root = _get_db(args)
    query = QueryEngine(db)
    ctx = query.get_context(args.name, kind=args.kind)

    if args.json:
        print(json.dumps(ctx.to_dict(), indent=2, default=str))
    else:
        print(formatter.format_context(ctx.to_dict()))
    db.close()


def cmd_impact(args):
    """Analyze impact of changing a symbol."""
    db, root = _get_db(args)
    query = QueryEngine(db)
    result = query.get_impact(args.name)

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(formatter.format_impact(result))
    db.close()


def cmd_search(args):
    """Search the index."""
    db, root = _get_db(args)
    query = QueryEngine(db)
    results = query.search(args.query, kind=args.kind, limit=args.limit)

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print(formatter.format_search(results))
    db.close()


def cmd_file(args):
    """Get file summary."""
    db, root = _get_db(args)
    query = QueryEngine(db)
    result = query.get_file_summary(args.path)

    if result:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"File not found: {args.path}")
    db.close()


def cmd_diagnostics(args):
    """Run or view diagnostics."""
    db, root = _get_db(args)
    rules = RuleEngine(db)

    if args.run:
        results = rules.run_all()
        for r in results:
            if r["findings_count"]:
                print(f"  {r['rule_id']}: {r['findings_count']} findings")
        total = sum(r["findings_count"] for r in results)
        print(f"\nTotal: {total} findings from {len(results)} rules")
    else:
        diags = db.get_diagnostics(
            severity=args.severity,
            rule_id=args.rule_id,
            limit=args.limit,
        )
        if args.json:
            print(json.dumps(diags, indent=2, default=str))
        else:
            print(formatter.format_diagnostics(diags))
    db.close()


def cmd_stats(args):
    """Show index statistics."""
    db, root = _get_db(args)
    stats = db.get_stats()
    print(formatter.format_stats(stats))
    db.close()


def cmd_serve(args):
    """Start the MCP server."""
    from ..server.mcp import MCPServer
    root = Path(args.project).resolve()
    server = MCPServer(root)
    server.run()


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="codeindex",
        description="AI-agent code intelligence tool",
    )
    parser.add_argument(
        "--project", "-p", default=".",
        help="Project root directory (default: current dir)",
    )
    parser.add_argument(
        "--json", "-j", action="store_true", default=False,
        help="Output as JSON",
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # init
    sub.add_parser("init", help="Build the full index")

    # update
    sub.add_parser("update", help="Incremental index update")

    # context
    p = sub.add_parser("context", help="Get context for a symbol")
    p.add_argument("name", help="Symbol name")
    p.add_argument("--kind", choices=["function", "method", "class"])

    # impact
    p = sub.add_parser("impact", help="Analyze impact of changing a symbol")
    p.add_argument("name", help="Symbol name")

    # search
    p = sub.add_parser("search", help="Search the index")
    p.add_argument("query", help="Search query")
    p.add_argument("--kind", choices=["function", "method", "class"])
    p.add_argument("--limit", type=int, default=20)

    # file
    p = sub.add_parser("file", help="Get file summary")
    p.add_argument("path", help="Relative file path")

    # diagnostics
    p = sub.add_parser("diagnostics", help="Run or view diagnostics")
    p.add_argument("--run", action="store_true", help="Run all rules")
    p.add_argument("--severity", choices=["error", "warning", "info"])
    p.add_argument("--rule-id", help="Filter by rule ID")
    p.add_argument("--limit", type=int, default=50)

    # stats
    sub.add_parser("stats", help="Show index statistics")

    # serve
    sub.add_parser("serve", help="Start MCP server")

    return parser


def run_cli(argv: Optional[list[str]] = None):
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return

    commands = {
        "init": cmd_init,
        "update": cmd_update,
        "context": cmd_context,
        "impact": cmd_impact,
        "search": cmd_search,
        "file": cmd_file,
        "diagnostics": cmd_diagnostics,
        "stats": cmd_stats,
        "serve": cmd_serve,
    }

    cmd = commands.get(args.command)
    if cmd:
        cmd(args)
    else:
        parser.print_help()
