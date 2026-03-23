# Codeindex Review — 2026-03-23 CLD CTRL (TypeScript/TSX)

## Session Summary
Used codeindex MCP tools from within a Claude Code session on the CLD CTRL project — a TypeScript/TSX monorepo (~94 source files) using React/Ink. Goal was to use `get_context`, `search`, and `callers` to understand call graphs while implementing a subfolder session nesting feature.

## Project Details
- **Language**: TypeScript + TSX (React components)
- **Build**: tsup bundler
- **Structure**: `packages/cli/src/` with `core/`, `tui/components/`, `tui/hooks/` subdirectories
- **File count**: 94 `.ts`/`.tsx` files

## Tools Used

### `index` (full)
- **Result**: 94 files found, **94 parse errors**, **0 symbols** extracted
- All files detected as `language: "typescript"` but every single one failed to parse
- No functions, classes, methods, calls, refs, or imports extracted
- Diagnostics ran but found nothing (no data to analyze)
- **Rating**: Not useful — complete parser failure on this project

### `search` (query: "buildProjectList")
- **Result**: Empty array `[]`
- Expected: At least the function definition in `projects.ts` and call sites in `App.tsx`, `useAppState.ts`, `index.ts`
- **Rating**: Not useful — returned nothing because index has no symbols

### `get_context` (name: "buildProjectListFast", kind: "function")
- **Result**: Empty object — no symbol, callers, callees, refs, annotations, diagnostics, or siblings
- Expected: Function definition, 3+ callers, callees like `discoverProjectsFast`, `readProjectIndex`, `disambiguateNames`
- **Rating**: Not useful — no data

### `file_summary` (path: "packages/cli/src/core/projects.ts")
- **Result**: Correctly identified the file (677 lines, language: typescript) but `symbols: []`, `imports: []`, `diagnostics: []`
- Shows the file discovery works but parsing doesn't
- **Rating**: Partially useful — confirms file exists and language detection works

## What Didn't Work
1. **TypeScript parser produced 94/94 parse errors.** The CLAUDE.md lists TypeScript/TSX as supported via tree-sitter, but every file in this project failed. Possible causes:
   - tree-sitter-typescript grammar not installed (`pip install tree-sitter-typescript` or similar)
   - Grammar version incompatible with the tree-sitter runtime version
   - TSX files (`.tsx`) not being routed to the TypeScript parser
   - TypeScript syntax features not supported (e.g., `satisfies`, `using`, `const` type parameters, or complex generic expressions)

2. **No error messages surfaced.** The index returned `"parse_errors": 94` but no detail about WHAT failed — no file path, line number, or error message. Debugging requires checking server logs or running the CLI directly. The MCP response should include at least the first few error details.

3. **Downstream tools silently return empty.** `search`, `get_context`, `callers` all return empty without any indication that the index is empty/broken. An agent (or user) could waste significant time calling tools that can never return results because the index failed. These tools should check if the index has symbols and warn if it's empty.

## MCP Tool Description Feedback

### Missing "run index first" guidance
The tool descriptions for `search`, `get_context`, `callers`, `get_impact`, and `file_summary` don't mention that `index` must be run first. An AI agent encountering codeindex for the first time will call `search` or `get_context` first (they sound like the primary tools), get empty results, and not know why.

**Suggestion**: Add to each query tool's description: *"Requires a built index — run `index` first if results are empty."* Or better: have query tools auto-detect an empty/missing index and return a message like `"No index found. Run the 'index' tool first."`

### `index` tool should surface parse errors
The current response shows `"parse_errors": 94` but no detail. When parse errors > 0, include at least:
- First 3-5 file paths that failed
- The parser that was used (tree-sitter-typescript vs fallback)
- A hint like "Check that tree-sitter-typescript is installed: `pip install tree-sitter-typescript`"

### Tool description for `get_context` says "THE primary tool"
This is good — it signals priority. But it should also mention it requires indexed data. Currently it reads: *"Get full context for a symbol: callers, callees, refs, annotations, diagnostics. THE primary tool for understanding code."*

**Suggestion**: *"...THE primary tool for understanding code. Returns empty if the symbol isn't indexed — run `index` first."*

## What Would Have Been Valuable (If It Worked)
The feature I was implementing (subfolder session nesting) touched 7 files across `core/` and `tui/`. I spent significant time manually tracing:
- Who calls `getRecentSessions` (to know what to update in App.tsx)
- Who calls `launchClaude` with a `projectPath` (to fix resume behavior)
- What imports `Session` type (to verify the new fields propagate)

`get_context` on `getRecentSessions` would have instantly shown all call sites. `get_impact` on the `Session` type would have shown every consumer. Instead I used Grep/Read manually — slower but functional.

## Suggestions for Improvement
1. **Parse error diagnostics**: Surface file-level error details in the `index` response
2. **Empty index warnings**: Query tools should detect and report empty indexes
3. **TypeScript support**: Investigate why 94/94 files fail — this is a common language for Claude Code users
4. **MCP descriptions**: Add "requires index" hints to all query tool descriptions
5. **Auto-index**: Consider auto-running incremental index on first query if no index exists
6. **Review path in MCP tool descriptions**: The MCP server should tell agents where to save feedback/reviews (e.g., `<project>/reviews/`). Currently an agent has no way to discover this path without being told by the user or finding the folder manually. Could be a field in the `session` tool's `end` action, or a dedicated `feedback` tool, or just mentioned in the top-level tool description for `diagnostics` or `session`.
