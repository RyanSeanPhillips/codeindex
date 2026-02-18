# codeindex

AI-agent code intelligence tool. Indexes your codebase into a structured SQLite database so AI coding agents (Claude Code, etc.) spend tokens on problem-solving, not searching.

## Install

```bash
pip install -e .
```

## Usage

```bash
# Index a project
codeindex init

# Get context for a symbol
codeindex context my_function

# Search the index
codeindex search "authentication"

# Run diagnostics
codeindex diagnostics

# Start MCP server (for Claude Code)
codeindex serve
```

## MCP Integration

Add to `.mcp.json`:

```json
{
  "mcpServers": {
    "codeindex": {
      "command": "codeindex",
      "args": ["serve"]
    }
  }
}
```
