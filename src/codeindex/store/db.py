"""
SQLite database layer for the code index.

WAL mode, foreign keys, transaction helpers, bulk inserts.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .models import (
    Annotation, Call, ChangeLog, Diagnostic, File, Import, IndexStats,
    Ref, Rule, RuleRun, Session, Symbol,
)
from .schema import INIT_META_SQL, SCHEMA_SQL, SCHEMA_VERSION


class Database:
    """SQLite code index database."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self._conn.executescript(SCHEMA_SQL)
        self._conn.execute(INIT_META_SQL, (str(SCHEMA_VERSION),))

    @contextmanager
    def transaction(self):
        self._conn.execute("BEGIN")
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _now(self) -> str:
        return datetime.now().isoformat()

    def close(self):
        self._conn.close()

    # ── File operations ──

    def upsert_file(self, f: File) -> File:
        self._conn.execute(
            """INSERT INTO files (rel_path, file_hash, language, line_count, parse_error, indexed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(rel_path) DO UPDATE SET
                 file_hash=excluded.file_hash,
                 language=excluded.language,
                 line_count=excluded.line_count,
                 parse_error=excluded.parse_error,
                 indexed_at=excluded.indexed_at""",
            (f.rel_path, f.file_hash, f.language, f.line_count, f.parse_error, f.indexed_at),
        )
        row = self._conn.execute(
            "SELECT file_id FROM files WHERE rel_path = ?", (f.rel_path,)
        ).fetchone()
        f.file_id = row["file_id"]
        return f

    def get_file_by_path(self, rel_path: str) -> Optional[File]:
        row = self._conn.execute(
            "SELECT * FROM files WHERE rel_path = ?", (rel_path,)
        ).fetchone()
        return self._row_to_file(row) if row else None

    def list_files(self) -> list[File]:
        rows = self._conn.execute("SELECT * FROM files ORDER BY rel_path").fetchall()
        return [self._row_to_file(r) for r in rows]

    def delete_file(self, file_id: int) -> bool:
        cur = self._conn.execute("DELETE FROM files WHERE file_id = ?", (file_id,))
        return cur.rowcount > 0

    def _row_to_file(self, row) -> File:
        return File(
            file_id=row["file_id"],
            rel_path=row["rel_path"],
            file_hash=row["file_hash"],
            language=row["language"],
            line_count=row["line_count"],
            parse_error=row["parse_error"],
            indexed_at=row["indexed_at"],
        )

    # ── Symbol operations ──

    def insert_symbol(self, s: Symbol) -> Symbol:
        cur = self._conn.execute(
            """INSERT INTO symbols
               (file_id, parent_id, kind, name, params_json, return_type,
                decorators_json, bases_json, docstring, line_start, line_end,
                complexity, is_async)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (s.file_id, s.parent_id, s.kind, s.name, s.params_json,
             s.return_type, s.decorators_json, s.bases_json, s.docstring,
             s.line_start, s.line_end, s.complexity, 1 if s.is_async else 0),
        )
        s.symbol_id = cur.lastrowid
        return s

    def bulk_insert_symbols(self, symbols: list[Symbol]) -> list[Symbol]:
        result = []
        for s in symbols:
            result.append(self.insert_symbol(s))
        return result

    def find_symbols(
        self,
        name: Optional[str] = None,
        kind: Optional[str] = None,
        file_pattern: Optional[str] = None,
        parent_name: Optional[str] = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT s.*, f.rel_path,
                   p.name as parent_name, p.kind as parent_kind
            FROM symbols s
            JOIN files f ON s.file_id = f.file_id
            LEFT JOIN symbols p ON s.parent_id = p.symbol_id
            WHERE 1=1
        """
        params: list = []
        if name:
            sql += " AND s.name LIKE ?"
            params.append(f"%{name}%")
        if kind:
            sql += " AND s.kind = ?"
            params.append(kind)
        if file_pattern:
            sql += " AND f.rel_path LIKE ?"
            params.append(f"%{file_pattern}%")
        if parent_name:
            sql += " AND p.name LIKE ?"
            params.append(f"%{parent_name}%")
        sql += " ORDER BY f.rel_path, s.line_start LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [self._symbol_row_to_dict(r) for r in rows]

    def _symbol_row_to_dict(self, row) -> dict[str, Any]:
        return {
            "symbol_id": row["symbol_id"],
            "kind": row["kind"],
            "name": row["name"],
            "parent_name": row["parent_name"],
            "parent_kind": row["parent_kind"],
            "params": json.loads(row["params_json"]),
            "return_type": row["return_type"],
            "decorators": json.loads(row["decorators_json"]),
            "bases": json.loads(row["bases_json"]),
            "docstring": row["docstring"],
            "line_start": row["line_start"],
            "line_end": row["line_end"],
            "complexity": row["complexity"],
            "is_async": bool(row["is_async"]),
            "file": row["rel_path"],
        }

    # ── Call operations ──

    def bulk_insert_calls(self, file_id: int, calls: list[Call]) -> None:
        self._conn.executemany(
            "INSERT INTO calls (file_id, caller_id, callee_expr, line_no) VALUES (?, ?, ?, ?)",
            [(file_id, c.caller_id, c.callee_expr, c.line_no) for c in calls],
        )

    def get_callers(self, function_name: str, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT c.*, f.rel_path,
                      s.name as caller_name, s.kind as caller_kind,
                      p.name as caller_class
               FROM calls c
               JOIN files f ON c.file_id = f.file_id
               LEFT JOIN symbols s ON c.caller_id = s.symbol_id
               LEFT JOIN symbols p ON s.parent_id = p.symbol_id
               WHERE c.callee_expr LIKE ?
               ORDER BY f.rel_path, c.line_no
               LIMIT ?""",
            (f"%{function_name}%", limit),
        ).fetchall()
        return [{
            "file": r["rel_path"],
            "line_no": r["line_no"],
            "callee_expr": r["callee_expr"],
            "caller_name": r["caller_name"],
            "caller_kind": r["caller_kind"],
            "caller_class": r["caller_class"],
        } for r in rows]

    def get_callees(self, symbol_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT c.callee_expr, c.line_no, f.rel_path
               FROM calls c
               JOIN files f ON c.file_id = f.file_id
               WHERE c.caller_id = ?
               ORDER BY c.line_no""",
            (symbol_id,),
        ).fetchall()
        return [{
            "callee_expr": r["callee_expr"],
            "line_no": r["line_no"],
            "file": r["rel_path"],
        } for r in rows]

    # ── Ref operations ──

    def bulk_insert_refs(self, file_id: int, refs: list[Ref]) -> None:
        self._conn.executemany(
            "INSERT INTO refs (file_id, symbol_id, ref_kind, target, name, line_no) VALUES (?, ?, ?, ?, ?, ?)",
            [(file_id, r.symbol_id, r.ref_kind, r.target, r.name, r.line_no) for r in refs],
        )

    # ── Import operations ──

    def bulk_insert_imports(self, file_id: int, imports: list[Import]) -> None:
        self._conn.executemany(
            "INSERT INTO imports (file_id, module, name, alias, is_from, line_no) VALUES (?, ?, ?, ?, ?, ?)",
            [(file_id, i.module, i.name, i.alias, 1 if i.is_from else 0, i.line_no) for i in imports],
        )

    # ── Rule operations ──

    def upsert_rule(self, r: Rule) -> None:
        self._conn.execute(
            """INSERT INTO rules (rule_id, name, description, severity, sql, is_builtin, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(rule_id) DO UPDATE SET
                 name=excluded.name, description=excluded.description,
                 severity=excluded.severity, sql=excluded.sql,
                 enabled=excluded.enabled""",
            (r.rule_id, r.name, r.description, r.severity, r.sql,
             1 if r.is_builtin else 0, 1 if r.enabled else 0, r.created_at or self._now()),
        )

    def get_rule(self, rule_id: str) -> Optional[Rule]:
        row = self._conn.execute("SELECT * FROM rules WHERE rule_id = ?", (rule_id,)).fetchone()
        if not row:
            return None
        return Rule(
            rule_id=row["rule_id"], name=row["name"], description=row["description"],
            severity=row["severity"], sql=row["sql"],
            is_builtin=bool(row["is_builtin"]), enabled=bool(row["enabled"]),
            created_at=row["created_at"],
        )

    def list_rules(self, enabled_only: bool = True) -> list[Rule]:
        sql = "SELECT * FROM rules"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY rule_id"
        rows = self._conn.execute(sql).fetchall()
        return [Rule(
            rule_id=r["rule_id"], name=r["name"], description=r["description"],
            severity=r["severity"], sql=r["sql"],
            is_builtin=bool(r["is_builtin"]), enabled=bool(r["enabled"]),
            created_at=r["created_at"],
        ) for r in rows]

    def insert_rule_run(self, run: RuleRun) -> None:
        self._conn.execute(
            "INSERT INTO rule_runs (rule_id, findings_count, useful_count, ran_at) VALUES (?, ?, ?, ?)",
            (run.rule_id, run.findings_count, run.useful_count, run.ran_at or self._now()),
        )

    # ── Diagnostic operations ──

    def bulk_insert_diagnostics(self, diagnostics: list[Diagnostic]) -> None:
        now = self._now()
        self._conn.executemany(
            """INSERT INTO diagnostics
               (file_id, rule_id, severity, message, line_no, context, is_resolved, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)""",
            [(d.file_id, d.rule_id, d.severity, d.message, d.line_no, d.context, now, now)
             for d in diagnostics],
        )

    def get_diagnostics(
        self,
        severity: Optional[str] = None,
        rule_id: Optional[str] = None,
        file_pattern: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        sql = """
            SELECT d.*, f.rel_path
            FROM diagnostics d
            JOIN files f ON d.file_id = f.file_id
            WHERE d.is_resolved = 0
        """
        params: list = []
        if severity:
            sql += " AND d.severity = ?"
            params.append(severity)
        if rule_id:
            sql += " AND d.rule_id = ?"
            params.append(rule_id)
        if file_pattern:
            sql += " AND f.rel_path LIKE ?"
            params.append(f"%{file_pattern}%")
        sql += " ORDER BY CASE d.severity WHEN 'error' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, f.rel_path, d.line_no LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(sql, params).fetchall()
        return [{
            "diag_id": r["diag_id"],
            "rule_id": r["rule_id"],
            "severity": r["severity"],
            "message": r["message"],
            "line_no": r["line_no"],
            "file": r["rel_path"],
        } for r in rows]

    def clear_diagnostics(self) -> None:
        self._conn.execute("DELETE FROM diagnostics")

    # ── Session operations ──

    def create_session(self, transcript_path: Optional[str] = None) -> Session:
        now = self._now()
        cur = self._conn.execute(
            "INSERT INTO sessions (started_at, transcript_path) VALUES (?, ?)",
            (now, transcript_path),
        )
        return Session(session_id=cur.lastrowid, started_at=now, transcript_path=transcript_path)

    def end_session(self, session_id: int, summary: Optional[str] = None) -> None:
        self._conn.execute(
            "UPDATE sessions SET ended_at = ?, summary = ? WHERE session_id = ?",
            (self._now(), summary, session_id),
        )

    def get_active_session(self) -> Optional[Session]:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE ended_at IS NULL ORDER BY session_id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return Session(
            session_id=row["session_id"], started_at=row["started_at"],
            ended_at=row["ended_at"], transcript_path=row["transcript_path"],
            summary=row["summary"],
        )

    def get_session(self, session_id: int) -> Optional[Session]:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if not row:
            return None
        return Session(
            session_id=row["session_id"], started_at=row["started_at"],
            ended_at=row["ended_at"], transcript_path=row["transcript_path"],
            summary=row["summary"],
        )

    def insert_change(self, change: ChangeLog) -> None:
        self._conn.execute(
            """INSERT INTO change_log (session_id, file_id, change_type, old_hash, new_hash, changed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (change.session_id, change.file_id, change.change_type,
             change.old_hash, change.new_hash, change.changed_at or self._now()),
        )

    def get_session_changes(self, session_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT cl.*, f.rel_path
               FROM change_log cl
               JOIN files f ON cl.file_id = f.file_id
               WHERE cl.session_id = ?
               ORDER BY cl.changed_at""",
            (session_id,),
        ).fetchall()
        return [{
            "file": r["rel_path"],
            "change_type": r["change_type"],
            "changed_at": r["changed_at"],
        } for r in rows]

    # ── Annotation operations ──

    def insert_annotation(self, a: Annotation) -> Annotation:
        cur = self._conn.execute(
            "INSERT INTO annotations (file_id, symbol_id, text, author, created_at) VALUES (?, ?, ?, ?, ?)",
            (a.file_id, a.symbol_id, a.text, a.author, a.created_at or self._now()),
        )
        a.annotation_id = cur.lastrowid
        return a

    def get_annotations(
        self,
        file_id: Optional[int] = None,
        symbol_id: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM annotations WHERE 1=1"
        params: list = []
        if file_id is not None:
            sql += " AND file_id = ?"
            params.append(file_id)
        if symbol_id is not None:
            sql += " AND symbol_id = ?"
            params.append(symbol_id)
        sql += " ORDER BY created_at"
        rows = self._conn.execute(sql, params).fetchall()
        return [{
            "annotation_id": r["annotation_id"],
            "text": r["text"],
            "author": r["author"],
            "created_at": r["created_at"],
        } for r in rows]

    # ── FTS operations ──

    def update_fts(self, rel_path: str, symbol_names: str, docstrings: str) -> None:
        self._conn.execute("DELETE FROM fts WHERE rel_path = ?", (rel_path,))
        self._conn.execute(
            "INSERT INTO fts (rel_path, symbol_names, docstrings) VALUES (?, ?, ?)",
            (rel_path, symbol_names, docstrings),
        )

    def search_fts(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        try:
            rows = self._conn.execute(
                """SELECT rel_path, symbol_names, docstrings, rank
                   FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT ?""",
                (query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [{
            "rel_path": r["rel_path"],
            "symbol_names": r["symbol_names"],
            "docstrings": r["docstrings"],
            "score": -r["rank"],
        } for r in rows]

    # ── Knowledge cache ──

    def set_knowledge(self, key: str, value: Any) -> None:
        self._conn.execute(
            """INSERT INTO knowledge (key, value_json, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at""",
            (key, json.dumps(value), self._now()),
        )

    def get_knowledge(self, key: str) -> Any:
        row = self._conn.execute("SELECT value_json FROM knowledge WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value_json"]) if row else None

    # ── File summary ──

    def get_file_summary(self, rel_path: str) -> Optional[dict[str, Any]]:
        f = self.get_file_by_path(rel_path)
        if not f:
            return None
        fid = f.file_id

        symbols = self._conn.execute(
            """SELECT s.*, p.name as parent_name
               FROM symbols s LEFT JOIN symbols p ON s.parent_id = p.symbol_id
               WHERE s.file_id = ? ORDER BY s.line_start""",
            (fid,),
        ).fetchall()

        imports = self._conn.execute(
            "SELECT * FROM imports WHERE file_id = ? ORDER BY line_no", (fid,)
        ).fetchall()

        diagnostics = self._conn.execute(
            "SELECT * FROM diagnostics WHERE file_id = ? AND is_resolved = 0 ORDER BY line_no", (fid,)
        ).fetchall()

        return {
            "file": {"rel_path": f.rel_path, "line_count": f.line_count, "language": f.language},
            "symbols": [{
                "kind": s["kind"],
                "name": s["name"],
                "parent_name": s["parent_name"],
                "line_start": s["line_start"],
                "line_end": s["line_end"],
                "complexity": s["complexity"],
                "docstring": s["docstring"],
            } for s in symbols],
            "imports": [{
                "module": i["module"],
                "name": i["name"],
                "is_from": bool(i["is_from"]),
                "line_no": i["line_no"],
            } for i in imports],
            "diagnostics": [{
                "rule_id": d["rule_id"],
                "severity": d["severity"],
                "message": d["message"],
                "line_no": d["line_no"],
            } for d in diagnostics],
        }

    # ── Stats ──

    def get_stats(self) -> IndexStats:
        def _count(table: str) -> int:
            return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

        diag_counts = {}
        for row in self._conn.execute(
            "SELECT severity, COUNT(*) as cnt FROM diagnostics WHERE is_resolved = 0 GROUP BY severity"
        ).fetchall():
            diag_counts[row["severity"]] = row["cnt"]

        class_count = self._conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE kind = 'class'"
        ).fetchone()[0]
        func_count = self._conn.execute(
            "SELECT COUNT(*) FROM symbols WHERE kind IN ('function', 'method')"
        ).fetchone()[0]
        parse_errors = self._conn.execute(
            "SELECT COUNT(*) FROM files WHERE parse_error IS NOT NULL"
        ).fetchone()[0]

        return IndexStats(
            total_files=_count("files"),
            total_symbols=_count("symbols"),
            total_classes=class_count,
            total_functions=func_count,
            total_calls=_count("calls"),
            total_refs=_count("refs"),
            total_imports=_count("imports"),
            total_diagnostics=_count("diagnostics"),
            errors=diag_counts.get("error", 0),
            warnings=diag_counts.get("warning", 0),
            parse_errors=parse_errors,
        )

    # ── Raw SQL (for rule engine) ──

    def execute_sql(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        """Execute a read-only SQL query and return results as dicts."""
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
