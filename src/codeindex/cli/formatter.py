"""
Human-readable output formatting for CLI.
"""

from __future__ import annotations

from typing import Any


def format_stats(stats: Any) -> str:
    """Format index stats for display."""
    if hasattr(stats, "total_files"):
        # IndexStats dataclass
        lines = [
            f"Files:       {stats.total_files}",
            f"Symbols:     {stats.total_symbols} ({stats.total_classes} classes, {stats.total_functions} functions)",
            f"Calls:       {stats.total_calls}",
            f"References:  {stats.total_refs}",
            f"Imports:     {stats.total_imports}",
            f"Diagnostics: {stats.total_diagnostics} ({stats.errors} errors, {stats.warnings} warnings)",
        ]
        if stats.parse_errors:
            lines.append(f"Parse errors: {stats.parse_errors}")
        return "\n".join(lines)
    # Dict form
    return "\n".join(f"{k}: {v}" for k, v in stats.items())


def format_context(ctx: dict) -> str:
    """Format symbol context for display."""
    lines = []
    sym = ctx.get("symbol", {})
    if not sym:
        return "Symbol not found."

    # Header
    kind = sym.get("kind", "")
    name = sym.get("name", "")
    parent = sym.get("parent_name", "")
    qual = f"{parent}.{name}" if parent else name
    file = sym.get("file", "")
    line_start = sym.get("line_start", 0)
    line_end = sym.get("line_end", 0)

    lines.append(f"{kind} {qual}")
    lines.append(f"  {file}:{line_start}-{line_end}")

    if sym.get("docstring"):
        doc = sym["docstring"][:100]
        lines.append(f"  \"{doc}\"")

    # Callers
    callers = ctx.get("callers", [])
    if callers:
        lines.append(f"\nCallers ({len(callers)}):")
        for c in callers[:10]:
            caller = c.get("caller_name", "?")
            cclass = c.get("caller_class", "")
            cqual = f"{cclass}.{caller}" if cclass else caller
            lines.append(f"  {cqual} -> {file}:{c.get('line_no', '?')}")

    # Callees
    callees = ctx.get("callees", [])
    if callees:
        lines.append(f"\nCallees ({len(callees)}):")
        for c in callees[:10]:
            lines.append(f"  {c.get('callee_expr', '?')} @ line {c.get('line_no', '?')}")

    # Diagnostics
    diags = ctx.get("diagnostics", [])
    if diags:
        lines.append(f"\nDiagnostics ({len(diags)}):")
        for d in diags:
            sev = d.get("severity", "?")
            lines.append(f"  [{sev}] {d.get('message', '?')} (line {d.get('line_no', '?')})")

    # Annotations
    anns = ctx.get("annotations", [])
    if anns:
        lines.append(f"\nAnnotations ({len(anns)}):")
        for a in anns:
            lines.append(f"  [{a.get('author', '?')}] {a.get('text', '')}")

    return "\n".join(lines)


def format_search(results: list[dict]) -> str:
    """Format search results for display."""
    if not results:
        return "No results found."

    lines = []
    for r in results:
        if r.get("type") == "symbol":
            kind = r.get("kind", "")
            name = r.get("name", "")
            parent = r.get("parent_name", "")
            qual = f"{parent}.{name}" if parent else name
            lines.append(f"  {kind:10s} {qual:40s} {r.get('file', '')}:{r.get('line_start', '')}")
        else:
            lines.append(f"  file       {r.get('rel_path', '')}")

    return f"Results ({len(results)}):\n" + "\n".join(lines)


def format_diagnostics(diags: list[dict]) -> str:
    """Format diagnostics for display."""
    if not diags:
        return "No diagnostics found."

    lines = [f"Diagnostics ({len(diags)}):"]
    for d in diags:
        sev = d.get("severity", "?")
        marker = {"error": "E", "warning": "W", "info": "I"}.get(sev, "?")
        file = d.get("file", "?")
        line_no = d.get("line_no", "?")
        lines.append(f"  [{marker}] {file}:{line_no} {d.get('rule_id', '')}: {d.get('message', '')}")

    return "\n".join(lines)


def format_impact(impact: dict) -> str:
    """Format impact analysis for display."""
    lines = [f"Impact analysis for: {impact.get('symbol', '?')}"]
    lines.append(f"Impact score: {impact.get('impact_score', 0):.1f}")

    direct = impact.get("direct_callers", [])
    if direct:
        lines.append(f"\nDirect callers ({len(direct)}):")
        for c in direct[:15]:
            lines.append(f"  {c.get('caller_name', '?')} in {c.get('file', '?')}:{c.get('line_no', '?')}")

    transitive = impact.get("transitive_callers", [])
    if transitive:
        lines.append(f"\nTransitive callers ({len(transitive)}):")
        for c in transitive[:10]:
            lines.append(f"  {c.get('caller_name', '?')} in {c.get('file', '?')}")

    files = impact.get("files_affected", [])
    if files:
        lines.append(f"\nFiles affected ({len(files)}):")
        for f in files:
            lines.append(f"  {f}")

    return "\n".join(lines)
