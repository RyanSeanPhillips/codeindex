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

from ..config import ProjectConfig
from ..core.indexer import Indexer
from ..core.query import QueryEngine
from ..core.differ import Differ
from ..rules.conventions import check_conventions
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


def _get_config(args) -> ProjectConfig:
    """Load project config from .codeindex.yaml."""
    project_root = Path(args.project).resolve()
    return ProjectConfig.load(project_root)


def cmd_init(args):
    """Build the full index."""
    db, root = _get_db(args)
    config = _get_config(args)
    indexer = Indexer(db, root, config=config)
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
    config = _get_config(args)
    indexer = Indexer(db, root, config=config)
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


def cmd_callers(args):
    """Show who calls a function/method."""
    db, root = _get_db(args)
    query = QueryEngine(db)
    callers = query.get_callers(args.name, limit=args.limit)

    if args.json:
        print(json.dumps(callers, indent=2, default=str))
    else:
        print(formatter.format_callers(callers, args.name))
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
        if args.json:
            # After running, return the findings filtered by path if specified
            diags = db.get_diagnostics(
                severity=args.severity,
                rule_id=args.rule_id,
                file_pattern=args.path,
                limit=args.limit,
            )
            print(json.dumps({"run_results": results, "diagnostics": diags}, indent=2, default=str))
        else:
            for r in results:
                if r["findings_count"]:
                    print(f"  {r['rule_id']}: {r['findings_count']} findings")
            total = sum(r["findings_count"] for r in results)
            print(f"\nTotal: {total} findings from {len(results)} rules")
    else:
        diags = db.get_diagnostics(
            severity=args.severity,
            rule_id=args.rule_id,
            file_pattern=args.path,
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


def cmd_conventions(args):
    """Check architectural layer boundary violations."""
    db, root = _get_db(args)
    config = _get_config(args)

    if not config.layers:
        print("No layers defined in .codeindex.yaml. Nothing to check.")
        db.close()
        return

    violations = check_conventions(db, config)
    if not violations:
        print(f"No layer violations found ({len(config.layers)} layers checked).")
    else:
        print(f"Layer violations ({len(violations)}):")
        for v in violations:
            print(f"  {v['file']}:{v['line_no']} - {v['message']}")
    db.close()


def cmd_serve(args):
    """Start the MCP server."""
    from ..server.mcp import MCPServer
    # Accept --project from either parent parser or serve subparser
    project = getattr(args, "project", None) or "."
    root = Path(project).resolve()
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

    # callers
    p = sub.add_parser("callers", help="Show who calls a function/method")
    p.add_argument("name", help="Function/method name")
    p.add_argument("--limit", type=int, default=50)

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
    p.add_argument("--path", help="Filter by file path (substring match)")
    p.add_argument("--limit", type=int, default=50)

    # stats
    sub.add_parser("stats", help="Show index statistics")

    # check-conventions
    sub.add_parser("check-conventions", help="Check layer boundary violations")

    # serve
    p = sub.add_parser("serve", help="Start MCP server")
    p.add_argument("--project", "-p", default=None, help="Project root (overrides global --project)")

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
        "callers": cmd_callers,
        "context": cmd_context,
        "impact": cmd_impact,
        "search": cmd_search,
        "file": cmd_file,
        "diagnostics": cmd_diagnostics,
        "stats": cmd_stats,
        "check-conventions": cmd_conventions,
        "serve": cmd_serve,
    }

    cmd = commands.get(args.command)
    if cmd:
        cmd(args)
    else:
        parser.print_help()
