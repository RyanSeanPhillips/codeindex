"""
Tests for the codeindex package.

Uses the fixture_project/ as a known codebase with predictable structure.
"""

import json
import pytest
from pathlib import Path

from codeindex.store.db import Database
from codeindex.store.models import IndexStats
from codeindex.core.indexer import Indexer
from codeindex.core.query import QueryEngine
from codeindex.core.differ import Differ
from codeindex.parsers.python import PythonParser
from codeindex.parsers.registry import get_parser
from codeindex.rules.engine import RuleEngine
from codeindex.sessions.tracker import SessionTracker

FIXTURE_DIR = Path(__file__).parent / "fixture_project"


@pytest.fixture
def db(tmp_path):
    """Create a fresh in-memory-like database."""
    db_path = tmp_path / "test.db"
    return Database(db_path)


@pytest.fixture
def indexed_db(db):
    """Database with the fixture project fully indexed."""
    indexer = Indexer(db, FIXTURE_DIR)
    indexer.full_rebuild()
    return db


# ── Parser tests ──

class TestPythonParser:
    def test_parse_simple_function(self):
        parser = PythonParser()
        result = parser.parse("def foo(x: int) -> str:\n    return str(x)\n", "test.py")
        assert result.parse_error is None
        assert len(result.symbols) == 1
        sym = result.symbols[0]
        assert sym.name == "foo"
        assert sym.kind == "function"
        params = json.loads(sym.params_json)
        assert params[0]["name"] == "x"
        assert sym.return_type is not None

    def test_parse_class_with_methods(self):
        parser = PythonParser()
        source = """
class MyClass:
    def method_one(self, x):
        pass
    def method_two(self):
        return 42
"""
        result = parser.parse(source, "test.py")
        classes = [s for s in result.symbols if s.kind == "class"]
        methods = [s for s in result.symbols if s.kind == "method"]
        assert len(classes) == 1
        assert classes[0].name == "MyClass"
        assert len(methods) == 2

    def test_parse_imports(self):
        parser = PythonParser()
        source = "import os\nfrom pathlib import Path\nfrom typing import Optional, List\n"
        result = parser.parse(source, "test.py")
        assert len(result.imports) >= 3  # os, Path, Optional, List

    def test_parse_calls(self):
        parser = PythonParser()
        source = "def foo():\n    bar()\n    baz(1, 2)\n"
        result = parser.parse(source, "test.py")
        assert len(result.calls) >= 2
        callee_names = {c.callee_expr for c in result.calls}
        assert "bar" in callee_names
        assert "baz" in callee_names

    def test_parse_async(self):
        parser = PythonParser()
        source = "async def fetch(url: str) -> bytes:\n    return b''\n"
        result = parser.parse(source, "test.py")
        assert len(result.symbols) == 1
        assert result.symbols[0].is_async is True

    def test_parse_syntax_error(self):
        parser = PythonParser()
        result = parser.parse("def foo(:\n", "bad.py")
        assert result.parse_error is not None

    def test_complexity(self):
        parser = PythonParser()
        source = """
def complex_func(x):
    if x > 0:
        for i in range(x):
            if i % 2:
                pass
    elif x < 0:
        while x < 0:
            x += 1
    return x
"""
        result = parser.parse(source, "test.py")
        assert result.symbols[0].complexity > 1


class TestRegistry:
    def test_python_detected(self):
        p = get_parser("foo.py")
        assert p is not None
        assert p.language == "python"

    def test_unknown_extension(self):
        p = get_parser("foo.rs")
        assert p is None


# ── Indexer tests ──

class TestIndexer:
    def test_discover_files(self, db):
        indexer = Indexer(db, FIXTURE_DIR)
        files = indexer.discover_files()
        rel_paths = {rel for _, rel in files}
        assert "main.py" in rel_paths
        assert "pkg/models.py" in rel_paths
        assert "pkg/utils.py" in rel_paths

    def test_full_rebuild(self, db):
        indexer = Indexer(db, FIXTURE_DIR)
        stats = indexer.full_rebuild()
        assert stats.total_files >= 3
        assert stats.total_symbols > 0
        assert stats.total_classes > 0
        assert stats.total_functions > 0

    def test_incremental_no_changes(self, indexed_db):
        indexer = Indexer(indexed_db, FIXTURE_DIR)
        result = indexer.incremental()
        assert result["changed"] == 0
        assert result["added"] == 0
        assert result["removed"] == 0


# ── Query tests ──

class TestQuery:
    def test_get_context(self, indexed_db):
        query = QueryEngine(indexed_db)
        ctx = query.get_context("main")
        assert ctx.symbol.get("name") == "main"
        assert ctx.symbol.get("kind") == "function"

    def test_get_context_class(self, indexed_db):
        query = QueryEngine(indexed_db)
        ctx = query.get_context("Application", kind="class")
        assert ctx.symbol.get("name") == "Application"

    def test_search(self, indexed_db):
        query = QueryEngine(indexed_db)
        results = query.search("helper")
        assert len(results) > 0
        names = {r.get("name", "") for r in results}
        assert "helper_function" in names

    def test_get_impact(self, indexed_db):
        query = QueryEngine(indexed_db)
        impact = query.get_impact("helper_function")
        assert "helper_function" == impact["symbol"]
        assert len(impact["direct_callers"]) >= 1

    def test_file_summary(self, indexed_db):
        query = QueryEngine(indexed_db)
        summary = query.get_file_summary("main.py")
        assert summary is not None
        assert summary["file"]["rel_path"] == "main.py"
        assert len(summary["symbols"]) > 0

    def test_callers(self, indexed_db):
        callers = indexed_db.get_callers("process_result")
        assert len(callers) >= 1
        # main() calls process_result()
        caller_names = {c["caller_name"] for c in callers}
        assert "main" in caller_names


# ── Rules tests ──

class TestRules:
    def test_seed_builtins(self, db):
        engine = RuleEngine(db)
        count = engine.seed_builtins()
        assert count == 3
        rules = db.list_rules()
        rule_ids = {r.rule_id for r in rules}
        assert "DEAD_SYMBOL" in rule_ids
        assert "LARGE_SYMBOL" in rule_ids
        assert "CIRCULAR_IMPORT" in rule_ids

    def test_run_all(self, indexed_db):
        engine = RuleEngine(indexed_db)
        engine.seed_builtins()
        results = engine.run_all()
        assert len(results) == 3
        # Should find at least the large method
        large = [r for r in results if r["rule_id"] == "LARGE_SYMBOL"]
        assert large[0]["findings_count"] >= 1

    def test_dead_symbol_detection(self, indexed_db):
        engine = RuleEngine(indexed_db)
        engine.seed_builtins()
        engine.run_all()
        diags = indexed_db.get_diagnostics(rule_id="DEAD_SYMBOL")
        dead_names = {d["message"] for d in diags}
        # dead_function should be flagged
        assert any("dead_function" in msg for msg in dead_names)

    def test_add_custom_rule(self, indexed_db):
        engine = RuleEngine(indexed_db)
        engine.seed_builtins()
        rule = engine.add_rule(
            "CUSTOM_TEST",
            "Test rule",
            "SELECT s.symbol_id, s.name, s.kind, s.line_start, f.rel_path, f.file_id "
            "FROM symbols s JOIN files f ON s.file_id = f.file_id WHERE s.name = 'main'",
            severity="info",
        )
        count = engine.run_one("CUSTOM_TEST")
        assert count >= 1

    def test_effectiveness_tracking(self, indexed_db):
        engine = RuleEngine(indexed_db)
        engine.seed_builtins()
        engine.run_all()
        engine.rate_rule("DEAD_SYMBOL", useful=True)
        eff = engine.get_effectiveness()
        assert len(eff) >= 3


# ── Session tests ──

class TestSessions:
    def test_start_end(self, db):
        tracker = SessionTracker(db)
        session = tracker.start()
        assert session.session_id > 0
        assert session.started_at

        active = tracker.get_active()
        assert active is not None
        assert active.session_id == session.session_id

        ended = tracker.end(summary="Test session")
        assert ended.ended_at is not None

        assert tracker.get_active() is None

    def test_auto_end_previous(self, db):
        tracker = SessionTracker(db)
        s1 = tracker.start()
        s2 = tracker.start()  # Should auto-end s1

        # s1 should be ended
        s1_check = db.get_session(s1.session_id)
        assert s1_check.ended_at is not None

        # s2 should be active
        active = tracker.get_active()
        assert active.session_id == s2.session_id


# ── Store tests ──

class TestStore:
    def test_file_crud(self, db):
        from codeindex.store.models import File
        f = db.upsert_file(File(rel_path="test.py", file_hash="abc", indexed_at="now"))
        assert f.file_id > 0

        retrieved = db.get_file_by_path("test.py")
        assert retrieved is not None
        assert retrieved.file_hash == "abc"

        db.delete_file(f.file_id)
        assert db.get_file_by_path("test.py") is None

    def test_annotations(self, indexed_db):
        from codeindex.store.models import Annotation
        symbols = indexed_db.find_symbols(name="main", limit=1)
        assert symbols

        ann = indexed_db.insert_annotation(Annotation(
            symbol_id=symbols[0]["symbol_id"],
            text="This is the entry point",
            author="test",
        ))
        assert ann.annotation_id > 0

        anns = indexed_db.get_annotations(symbol_id=symbols[0]["symbol_id"])
        assert len(anns) == 1
        assert anns[0]["text"] == "This is the entry point"

    def test_knowledge_cache(self, db):
        db.set_knowledge("test_key", {"foo": "bar"})
        val = db.get_knowledge("test_key")
        assert val == {"foo": "bar"}

    def test_fts_search(self, indexed_db):
        results = indexed_db.search_fts("helper")
        assert len(results) > 0
