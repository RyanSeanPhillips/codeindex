"""
Rule engine — execute SQL-based analysis rules with effectiveness tracking.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ..store.db import Database
from ..store.models import Diagnostic, Rule, RuleRun
from .builtin import BUILTIN_RULES


class RuleEngine:
    """Execute analysis rules and track their effectiveness."""

    def __init__(self, db: Database):
        self.db = db

    def seed_builtins(self) -> int:
        """Register built-in rules. Returns count of rules seeded."""
        count = 0
        for rule in BUILTIN_RULES:
            rule.created_at = datetime.now().isoformat()
            self.db.upsert_rule(rule)
            count += 1
        return count

    def run_all(self) -> list[dict[str, Any]]:
        """Run all enabled rules. Returns list of {rule_id, findings_count}."""
        self.db.clear_diagnostics()
        rules = self.db.list_rules(enabled_only=True)
        results = []

        for rule in rules:
            findings = self._run_rule(rule)
            results.append({
                "rule_id": rule.rule_id,
                "name": rule.name,
                "severity": rule.severity,
                "findings_count": findings,
            })

        return results

    def run_one(self, rule_id: str) -> int:
        """Run a single rule. Returns findings count."""
        rule = self.db.get_rule(rule_id)
        if not rule:
            raise ValueError(f"Unknown rule: {rule_id}")
        return self._run_rule(rule)

    def _run_rule(self, rule: Rule) -> int:
        """Execute a rule's SQL and store diagnostics. Returns findings count."""
        try:
            rows = self.db.execute_sql(rule.sql)
        except Exception as e:
            # Bad SQL — record the error as a diagnostic on a dummy file
            return 0

        diagnostics = []
        for row in rows:
            file_id = row.get("file_id", 0)
            if not file_id:
                continue

            # Build message from available columns
            msg = self._build_message(rule, row)
            line_no = row.get("line_start") or row.get("line_no")
            context = row.get("rel_path", "")

            diagnostics.append(Diagnostic(
                file_id=file_id,
                rule_id=rule.rule_id,
                severity=rule.severity,
                message=msg,
                line_no=line_no,
                context=context,
            ))

        if diagnostics:
            self.db.bulk_insert_diagnostics(diagnostics)

        # Record the run
        self.db.insert_rule_run(RuleRun(
            rule_id=rule.rule_id,
            findings_count=len(diagnostics),
            ran_at=datetime.now().isoformat(),
        ))

        return len(diagnostics)

    def _build_message(self, rule: Rule, row: dict) -> str:
        """Build a human-readable message from a rule result row."""
        name = row.get("name", "")
        parent_name = row.get("parent_name", "")
        kind = row.get("kind", "")
        rel_path = row.get("rel_path", "")

        qual_name = f"{parent_name}.{name}" if parent_name else name

        if rule.rule_id == "DEAD_SYMBOL":
            return f"{qual_name} ({kind}) -- never called"
        elif rule.rule_id == "LARGE_SYMBOL":
            line_start = row.get("line_start", 0)
            line_end = row.get("line_end", 0)
            cx = row.get("complexity", 0)
            lines = line_end - line_start
            parts = []
            if lines > 50:
                parts.append(f"{lines} lines")
            if cx > 15:
                parts.append(f"complexity {cx}")
            return f"{qual_name}: {', '.join(parts)}"
        elif rule.rule_id == "CIRCULAR_IMPORT":
            file_a = row.get("file_a", "")
            file_b = row.get("file_b", "")
            return f"Circular import: {file_a} <-> {file_b}"
        else:
            # Generic message for custom rules
            return f"{rule.name}: {qual_name}" if qual_name else rule.name

    def add_rule(self, rule_id: str, name: str, sql: str,
                 severity: str = "warning", description: str = "",
                 weight: float = 1.0, learned_from: Optional[str] = None) -> Rule:
        """Add a custom analysis rule."""
        rule = Rule(
            rule_id=rule_id,
            name=name,
            description=description,
            severity=severity,
            sql=sql,
            is_builtin=False,
            enabled=True,
            created_at=datetime.now().isoformat(),
            weight=weight,
            learned_from=learned_from,
        )
        self.db.upsert_rule(rule)
        return rule

    def test_rule(self, sql: str) -> list[dict[str, Any]]:
        """Dry-run a rule SQL without storing results. Returns raw query output."""
        try:
            rows = self.db.execute_sql(sql)
            return rows[:50]  # Cap preview at 50 rows
        except Exception as e:
            return [{"error": str(e)}]

    def rate_rule(self, rule_id: str, useful: bool) -> None:
        """Rate the most recent run of a rule as useful or not."""
        row = self.db._conn.execute(
            "SELECT run_id, useful_count FROM rule_runs WHERE rule_id = ? ORDER BY run_id DESC LIMIT 1",
            (rule_id,),
        ).fetchone()
        if row:
            new_count = row["useful_count"] + (1 if useful else -1)
            self.db._conn.execute(
                "UPDATE rule_runs SET useful_count = ? WHERE run_id = ?",
                (new_count, row["run_id"]),
            )

    def get_effectiveness(self) -> list[dict[str, Any]]:
        """Get effectiveness stats for all rules."""
        rows = self.db._conn.execute("""
            SELECT r.rule_id, r.name, r.severity, r.is_builtin,
                   COUNT(rr.run_id) as total_runs,
                   COALESCE(SUM(rr.findings_count), 0) as total_findings,
                   COALESCE(SUM(rr.useful_count), 0) as total_useful
            FROM rules r
            LEFT JOIN rule_runs rr ON r.rule_id = rr.rule_id
            GROUP BY r.rule_id
            ORDER BY total_useful DESC, total_findings DESC
        """).fetchall()
        return [dict(r) for r in rows]
