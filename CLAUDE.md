# codeindex

AI-agent code intelligence tool. Indexes codebases into a structured SQLite database, queryable via MCP tools or CLI.

## Quick Reference

- **Language**: Python 3.10+
- **Parsers**: Python (tree-sitter + ast fallback), TypeScript/TSX, PowerShell, C, C++, C#, Go, Rust, Java (all via tree-sitter, optional deps)
- **Database**: Single `.codeindex.db` SQLite file per project (WAL mode)
- **Protocol**: MCP via JSON-RPC 2.0 stdio
- **Tests**: `pytest tests/ -v`
- **Install**: `pip install -e .`
- **CLI entry**: `codeindex` console script -> `__main__.py` -> `cli.commands.run_cli()`

## Architecture

```
src/codeindex/
  __main__.py          # Entry point
  config.py            # .codeindex.yaml loader, LayerConfig, ProjectConfig
  cli/
    commands.py        # 10+ argparse subcommands (init, update, context, impact, search, ...)
    formatter.py       # Terminal output formatting
  core/
    indexer.py         # File discovery, parsing, index building (full + incremental)
    query.py           # get_context, get_impact, search, file_summary, get_callers
    differ.py          # File change tracking across sessions
  parsers/
    base.py            # Abstract LanguageParser interface, ParseResult dataclass
    python.py          # Tree-sitter + AST fallback for Python
    typescript.py      # TypeScript/TSX parser (tree-sitter)
    powershell.py      # PowerShell parser (tree-sitter)
    c_lang.py          # C parser (tree-sitter)
    cpp.py             # C++ parser (tree-sitter)
    csharp.py          # C# parser (tree-sitter)
    go.py              # Go parser (tree-sitter)
    rust.py            # Rust parser (tree-sitter)
    java.py            # Java parser (tree-sitter)
    registry.py        # Parser discovery by file extension (auto-loads available parsers)
  store/
    models.py          # Dataclasses: File, Symbol, Call, Ref, Import, Rule, Diagnostic, etc.
    db.py              # SQLite layer, transactions, CRUD ops, FTS
    schema.py          # DDL for all 12 tables + FTS5 virtual table
  rules/
    engine.py          # RuleEngine: run SQL rules, effectiveness tracking
    builtin.py         # 3 built-in rules: DEAD_SYMBOL, LARGE_SYMBOL, CIRCULAR_IMPORT
    conventions.py     # Architectural layer boundary checker
    seed.py            # Extract rules from config or CLAUDE.md
  sessions/
    tracker.py         # Start/end sessions, link to conversation transcripts
    history.py         # Record file changes per session
  server/
    mcp.py             # MCP server (JSON-RPC 2.0 stdio), 8 tools
tests/
  test_codeindex.py    # Comprehensive test suite
  fixture_project/     # Deterministic test codebase
```

## Key Design Decisions

- **Unified symbol table**: Single `symbols` table for classes, functions, and methods
- **Hash-based incremental indexing**: SHA256 file hashes for change detection
- **Agent-writable rules**: Agents create SQL-based analysis rules and track their effectiveness
- **Session awareness**: Links file changes to conversation transcripts for cross-session continuity
- **Tree-sitter primary, ast fallback**: Fast incremental parsing with robustness guarantee

## Language Support

**9 languages supported** via tree-sitter parsers. Python is included by default; others are optional dependencies:

| Language | Package | Extensions |
|----------|---------|------------|
| Python | tree-sitter-python (included) | .py |
| TypeScript | tree-sitter-typescript | .ts, .tsx |
| PowerShell | tree-sitter-powershell | .ps1, .psm1, .psd1 |
| C | tree-sitter-c | .c, .h |
| C++ | tree-sitter-cpp | .cpp, .cxx, .cc, .hpp, .hxx, .hh |
| C# | tree-sitter-c-sharp | .cs |
| Go | tree-sitter-go | .go |
| Rust | tree-sitter-rust | .rs |
| Java | tree-sitter-java | .java |

Install all: `pip install -e ".[all-languages]"` or individual: `pip install -e ".[typescript,powershell]"`

Adding more languages requires: implementing `LanguageParser` (see `parsers/base.py`), registering in `parsers/registry.py`.

## MCP Tools (10 tools)

| Tool | Purpose |
|------|---------|
| `index` | Build/update the code index (full or incremental) |
| `get_context` | **Primary tool** — full context for a symbol: callers, callees, refs, annotations, diagnostics |
| `callers` | Who calls this function/method? |
| `get_impact` | What breaks if I change this? Direct + transitive callers |
| `search` | Full-text + structured search across symbols and docstrings |
| `file_summary` | Structured overview of a file: symbols, imports, diagnostics |
| `diagnostics` | Run/create/rate analysis rules, view findings |
| `check_conventions` | Check architectural layer boundary violations |
| `annotate` | Persistent notes on symbols or files |
| `session` | Manage coding sessions, view changes, link transcripts |

## Built-in Analysis Rules

1. **DEAD_SYMBOL** — Functions/methods with no callers
2. **LARGE_SYMBOL** — Functions > 50 lines or complexity > 15
3. **CIRCULAR_IMPORT** — Module A imports B and B imports A

## Running

```bash
# Index a project
codeindex init

# Incremental update
codeindex update

# Query
codeindex context my_function
codeindex impact my_function
codeindex search "authentication"

# Start MCP server (for Claude Code integration)
codeindex serve
```

## MCP Integration

Add to project's `.mcp.json`:
```json
{
  "mcpServers": {
    "codeindex": {
      "command": "codeindex",
      "args": ["serve", "--project", "."]
    }
  }
}
```

## Testing

```bash
pytest tests/ -v
```

Tests cover: parser extraction, registry, indexer (full + incremental), query engine, rule engine, custom rules, effectiveness tracking, sessions, and change tracking. Uses `tests/fixture_project/` as a deterministic test codebase.
