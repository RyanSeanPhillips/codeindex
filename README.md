# codeindex

**AI-agent code intelligence tool.** Indexes your codebase into a structured SQLite database so AI coding agents (Claude Code, Cursor, etc.) spend tokens on problem-solving, not searching.

## Why This Exists

AI coding agents waste significant context window on codebase exploration -- running grep, reading files, building mental models of call graphs. `codeindex` front-loads this work into a persistent, structured index that the agent queries via MCP tools. The result:

- **Instant context**: "Who calls `load_file`?" answered from an index, not grep
- **Impact analysis**: "What breaks if I change this?" via transitive caller graphs
- **AI-writable rules**: The agent writes its own static analysis rules as SQL, tracks their effectiveness, and evolves them over time
- **Session awareness**: Track what changed since the last coding session, link changes to conversation transcripts
- **Architectural enforcement**: Define layers in config, catch boundary violations automatically

## How an AI Agent Uses This

When configured as an MCP server, the agent has access to these tools:

| Tool | What it does | When to use |
|------|-------------|-------------|
| `index` | Build/update the code index | Start of session, after file changes |
| `get_context` | Full context for a symbol: callers, callees, refs, annotations, diagnostics | **Primary tool** -- before modifying any function |
| `get_impact` | What breaks if I change this? Direct + transitive callers, affected files | Before refactoring |
| `search` | FTS + structured search across symbols, files, docstrings | Finding relevant code |
| `file_summary` | Structured overview: symbols, imports, diagnostics | Understanding a file |
| `diagnostics` | Run rules, view findings, add/rate/test rules | Code quality, writing new rules |
| `check_conventions` | Check architectural layer boundary violations | After adding imports |
| `annotate` | Persistent notes on symbols/files (survive re-indexing) | Documenting decisions |
| `session` | Start/end sessions, view changes, link transcripts | Session bookkeeping |

### Typical Agent Workflow

```
1. Start session       -> session(action="start")
2. Index codebase      -> index(mode="incremental")
3. Understand target   -> get_context(name="load_file")
4. Check impact        -> get_impact(name="load_file")
5. Make changes        -> [edit files normally]
6. Run diagnostics     -> diagnostics(action="run")
7. Write a new rule    -> diagnostics(action="add_rule", rule_id="NO_SELF_MW",
                            rule_sql="SELECT ... WHERE ...",
                            learned_from="CLAUDE.md")
8. End session         -> session(action="end", summary="Refactored file loading")
```

### AI-Writable Rules (Key Innovation)

The agent can write its own static analysis rules as SQL queries against the index schema. This lets the agent encode project-specific conventions it discovers:

```
# Test a rule before committing it
diagnostics(action="test_rule", rule_sql="SELECT s.symbol_id as file_id, ...")

# Add the rule with provenance
diagnostics(action="add_rule",
    rule_id="HEAVY_IMPORT_AT_MODULE_LEVEL",
    rule_name="Heavy import at module level",
    rule_sql="SELECT f.file_id, f.rel_path, i.module, i.line_no
              FROM imports i JOIN files f ON i.file_id = f.file_id
              WHERE i.module IN ('scipy', 'sklearn', 'matplotlib')
              AND i.line_no < 20",
    severity="warning",
    learned_from="CLAUDE.md")

# After using the findings, rate effectiveness
diagnostics(action="rate", rule_id="HEAVY_IMPORT_AT_MODULE_LEVEL", useful=true)
```

Rules with higher `useful` ratings surface first. Rules that consistently produce false positives can be disabled or refined.

## Install

```bash
pip install -e .
```

**Dependencies**: `tree-sitter`, `tree-sitter-python`, `pathspec`. Optional: `pyyaml` for config files.

## CLI Usage

```bash
# Index a project
codeindex init

# Incremental update (only changed files)
codeindex update

# Get context for a symbol
codeindex context my_function
codeindex context MyClass --kind class

# Impact analysis
codeindex impact load_file

# Search
codeindex search "authentication"

# Diagnostics
codeindex diagnostics              # view findings
codeindex diagnostics --run        # run all rules
codeindex diagnostics --severity error

# Check architectural conventions
codeindex check-conventions

# Stats
codeindex stats

# Start MCP server
codeindex serve
```

## MCP Integration (Claude Code)

Add to your project's `.mcp.json`:

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

## Configuration (.codeindex.yaml)

Optional config file in project root:

```yaml
project:
  name: MyProject
  repo: https://github.com/user/repo
  instructions: CLAUDE.md

# Extra ignore patterns (augments .gitignore)
ignore:
  - "lib/"
  - "dev_testing/"

# Architectural layers with import constraints
layers:
  - name: domain
    paths: ["core/domain/**"]
    allowed_imports: []
    description: "Pure domain models, no external deps"
  - name: services
    paths: ["core/services/**"]
    allowed_imports: [domain]
  - name: views
    paths: ["views/**", "dialogs/**"]
    allowed_imports: [services, domain]

# Files to seed rules from
seed_rules_from:
  - CLAUDE.md
```

## Schema Overview

The index stores everything in a single `.codeindex.db` SQLite file:

| Table | Purpose |
|-------|---------|
| `files` | Source files with hashes for change detection |
| `symbols` | Unified: functions, methods, classes (with params, docstrings, complexity) |
| `calls` | Call graph edges (caller_id -> callee_expr) |
| `refs` | Attribute references (read/write/import/type_ref) |
| `imports` | Import statements with module resolution |
| `rules` | Analysis rules (SQL queries) with weight and provenance |
| `rule_runs` | Execution history with effectiveness ratings |
| `diagnostics` | Findings from rules with resolution tracking |
| `sessions` | Coding sessions linked to conversation transcripts |
| `change_log` | File changes per session |
| `annotations` | Persistent notes on symbols/files |
| `knowledge` | Key-value store for metadata |
| `fts` | FTS5 full-text search on symbol names and docstrings |

## Development

```bash
# Run tests
pytest tests/ -v

# Index this project (meta!)
codeindex init -p .
```

## License

MIT
