"""
DDL for the code index SQLite database.

Single file, WAL mode, FTS5 for full-text search.
"""

SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- Source files
CREATE TABLE IF NOT EXISTS files (
    file_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path    TEXT NOT NULL UNIQUE,
    file_hash   TEXT NOT NULL,
    language    TEXT NOT NULL DEFAULT 'python',
    line_count  INTEGER NOT NULL DEFAULT 0,
    parse_error TEXT,
    indexed_at  TEXT NOT NULL
);

-- Unified symbols: class, function, method, interface, enum
CREATE TABLE IF NOT EXISTS symbols (
    symbol_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files ON DELETE CASCADE,
    parent_id   INTEGER REFERENCES symbols ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'function',
    name        TEXT NOT NULL,
    params_json TEXT DEFAULT '[]',
    return_type TEXT,
    decorators_json TEXT DEFAULT '[]',
    bases_json  TEXT DEFAULT '[]',
    docstring   TEXT,
    line_start  INTEGER NOT NULL,
    line_end    INTEGER NOT NULL,
    complexity  INTEGER DEFAULT 0,
    is_async    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_parent ON symbols(parent_id);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);

-- Call sites
CREATE TABLE IF NOT EXISTS calls (
    call_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files ON DELETE CASCADE,
    caller_id   INTEGER REFERENCES symbols ON DELETE CASCADE,
    callee_expr TEXT NOT NULL,
    line_no     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_expr);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);

-- Unified references: read, write, call, import, type_ref
CREATE TABLE IF NOT EXISTS refs (
    ref_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files ON DELETE CASCADE,
    symbol_id   INTEGER REFERENCES symbols ON DELETE CASCADE,
    ref_kind    TEXT NOT NULL DEFAULT 'read',
    target      TEXT NOT NULL,
    name        TEXT NOT NULL,
    line_no     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refs_target ON refs(target, name);
CREATE INDEX IF NOT EXISTS idx_refs_symbol ON refs(symbol_id);

-- Imports
CREATE TABLE IF NOT EXISTS imports (
    import_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files ON DELETE CASCADE,
    module      TEXT NOT NULL,
    name        TEXT,
    alias       TEXT,
    is_from     INTEGER NOT NULL DEFAULT 0,
    line_no     INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_imports_module ON imports(module);

-- Analysis rules (SQL queries with effectiveness tracking)
CREATE TABLE IF NOT EXISTS rules (
    rule_id     TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    severity    TEXT NOT NULL DEFAULT 'warning',
    sql         TEXT NOT NULL,
    is_builtin  INTEGER NOT NULL DEFAULT 1,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- Rule execution history
CREATE TABLE IF NOT EXISTS rule_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id         TEXT NOT NULL REFERENCES rules ON DELETE CASCADE,
    findings_count  INTEGER NOT NULL DEFAULT 0,
    useful_count    INTEGER NOT NULL DEFAULT 0,
    ran_at          TEXT NOT NULL
);

-- Diagnostics (findings from rules)
CREATE TABLE IF NOT EXISTS diagnostics (
    diag_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     INTEGER NOT NULL REFERENCES files ON DELETE CASCADE,
    rule_id     TEXT NOT NULL,
    severity    TEXT NOT NULL DEFAULT 'warning',
    message     TEXT NOT NULL,
    line_no     INTEGER,
    context     TEXT,
    is_resolved INTEGER NOT NULL DEFAULT 0,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_diag_rule ON diagnostics(rule_id);

-- Sessions
CREATE TABLE IF NOT EXISTS sessions (
    session_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    ended_at        TEXT,
    transcript_path TEXT,
    summary         TEXT
);

-- Change log (file changes per session)
CREATE TABLE IF NOT EXISTS change_log (
    change_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions ON DELETE CASCADE,
    file_id     INTEGER NOT NULL REFERENCES files ON DELETE CASCADE,
    change_type TEXT NOT NULL,
    old_hash    TEXT,
    new_hash    TEXT,
    changed_at  TEXT NOT NULL
);

-- Annotations (persistent notes on symbols/files)
CREATE TABLE IF NOT EXISTS annotations (
    annotation_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id         INTEGER REFERENCES files ON DELETE CASCADE,
    symbol_id       INTEGER REFERENCES symbols ON DELETE CASCADE,
    text            TEXT NOT NULL,
    author          TEXT NOT NULL DEFAULT 'user',
    created_at      TEXT NOT NULL
);

-- Persistent key-value store
CREATE TABLE IF NOT EXISTS knowledge (
    key         TEXT PRIMARY KEY,
    value_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Full-text search on symbols
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    rel_path, symbol_names, docstrings,
    tokenize='porter unicode61'
);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

INIT_META_SQL = """
INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?);
"""
