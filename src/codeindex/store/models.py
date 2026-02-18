"""
Domain models for the code index.

Pure dataclasses — no external dependencies. Each maps to a SQLite table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Any


@dataclass
class File:
    """A source file in the index."""
    file_id: int = 0
    rel_path: str = ""
    file_hash: str = ""
    language: str = "python"
    line_count: int = 0
    parse_error: Optional[str] = None
    indexed_at: str = ""


@dataclass
class Symbol:
    """A named code entity — function, method, class, interface, enum.

    Unified table replacing separate classes/functions tables.
    """
    symbol_id: int = 0
    file_id: int = 0
    parent_id: Optional[int] = None  # class_id for methods
    kind: str = "function"  # function, method, class, interface, enum
    name: str = ""
    params_json: str = "[]"  # JSON list of param dicts
    return_type: Optional[str] = None
    decorators_json: str = "[]"  # JSON list of decorator strings
    bases_json: str = "[]"  # JSON list of base class strings (for classes)
    docstring: Optional[str] = None
    line_start: int = 0
    line_end: int = 0
    complexity: int = 0
    is_async: bool = False


@dataclass
class Call:
    """A function call site."""
    call_id: int = 0
    file_id: int = 0
    caller_id: Optional[int] = None  # symbol_id of enclosing function
    callee_expr: str = ""
    line_no: int = 0


@dataclass
class Ref:
    """A reference — read, write, import, type_ref.

    Unified table replacing separate attribute_access/signals/connections tables.
    """
    ref_id: int = 0
    file_id: int = 0
    symbol_id: Optional[int] = None  # enclosing function/method
    ref_kind: str = "read"  # read, write, call, import, type_ref
    target: str = ""  # e.g. 'self.state', 'os.path'
    name: str = ""  # attribute/member name
    line_no: int = 0


@dataclass
class Import:
    """An import statement."""
    import_id: int = 0
    file_id: int = 0
    module: str = ""
    name: Optional[str] = None
    alias: Optional[str] = None
    is_from: bool = False
    line_no: int = 0


@dataclass
class Rule:
    """An analysis rule — SQL query with effectiveness tracking."""
    rule_id: str = ""
    name: str = ""
    description: str = ""
    severity: str = "warning"  # error, warning, info
    sql: str = ""
    is_builtin: bool = True
    enabled: bool = True
    created_at: str = ""
    weight: float = 1.0  # importance multiplier (AI-adjustable)
    learned_from: Optional[str] = None  # e.g. "CLAUDE.md", "user feedback", "session:42"


@dataclass
class RuleRun:
    """A record of running a rule — for effectiveness tracking."""
    run_id: int = 0
    rule_id: str = ""
    findings_count: int = 0
    useful_count: int = 0  # user-rated as useful
    ran_at: str = ""


@dataclass
class Diagnostic:
    """A finding from running a rule."""
    diag_id: int = 0
    file_id: int = 0
    rule_id: str = ""
    severity: str = "warning"
    message: str = ""
    line_no: Optional[int] = None
    context: Optional[str] = None
    is_resolved: bool = False
    first_seen: str = ""
    last_seen: str = ""


@dataclass
class Session:
    """A coding session — linked to a conversation transcript."""
    session_id: int = 0
    started_at: str = ""
    ended_at: Optional[str] = None
    transcript_path: Optional[str] = None
    summary: Optional[str] = None


@dataclass
class ChangeLog:
    """A file change within a session."""
    change_id: int = 0
    session_id: int = 0
    file_id: int = 0
    change_type: str = ""  # added, modified, deleted
    old_hash: Optional[str] = None
    new_hash: Optional[str] = None
    changed_at: str = ""


@dataclass
class Annotation:
    """A persistent note on a symbol or file."""
    annotation_id: int = 0
    file_id: Optional[int] = None
    symbol_id: Optional[int] = None
    text: str = ""
    author: str = ""  # 'user' or 'ai'
    created_at: str = ""


@dataclass
class IndexStats:
    """Summary statistics for the index."""
    total_files: int = 0
    total_symbols: int = 0
    total_classes: int = 0
    total_functions: int = 0
    total_calls: int = 0
    total_refs: int = 0
    total_imports: int = 0
    total_diagnostics: int = 0
    errors: int = 0
    warnings: int = 0
    parse_errors: int = 0
