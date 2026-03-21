# Codeindex Review: Bonsai Tracker Pre-Experiment Deep Review

**Date**: 2026-03-09
**Task**: Deep code review of Three Chamber Tracker app for UI/UX and data accuracy issues, then design implementation plan for ~20 enhancements (new CSV columns, metrics module, summary stats, stim visualization, drawing UX)
**Codebase**: three_chamber_app (~15 files, 512 symbols, 33 classes, 479 functions)
**Agent**: Claude Code (Opus 4.6)
**Phases covered**: Planning only (code review + implementation design)

## Task Summary

User needed a comprehensive review of their rodent tracking app before using it for real experiments. The review identified 14 issues across data accuracy and UX, then produced an implementation plan for ~20 changes across 4 phases. The codeindex was used during the planning phase to understand call chains, symbol relationships, and change impact.

## Usage Summary

| Tool | Calls | Value Rating | Notes |
|------|-------|-------------|-------|
| `index` (full) | 1 | High | Fast, gave useful codebase stats |
| `get_impact` | 2 | **Very High** | Showed full call chains for `write_row` and `process_frame` |
| `get_context` | 4 | **Very High** | Deep symbol info for `TrackingCSVWriter`, `CameraThread`, `update_stats`, `_record_frame` |
| `search` | 1 | **Low** | Returned empty for natural language query |
| `file_summary` | 0 | N/A | Didn't use — Read tool was preferred for full file contents |
| `annotate` | 0 | N/A | Didn't use |
| `session` | 0 | N/A | Didn't use |
| `diagnostics` | 0 | N/A | Ran automatically with index, didn't use further |
| `check_conventions` | 0 | N/A | Didn't use |
| `callers` | 0 | N/A | `get_context` already includes callers, so this was redundant |
| **Total** | **8** | | |

For comparison, 3 Explore agents were also launched (reading ~12 files total), plus 3 direct Read calls. The codeindex was ~15% of the information gathering but punched above its weight on the calls that worked.

---

## What Worked Well

### `get_impact` — Best Tool for Change Planning (2 calls, both very high value)

**Call 1: `get_impact("write_row")`**
Immediately showed:
- Direct callers: `_record_frame` in `camera.py:453`
- Transitive callers: `_run_camera_loop.run()` and `_run_video_file.run()`
- Files affected: `camera.py` (+ backup)
- Impact score: 5.0

This told me exactly what I needed: changing `write_row()` signature only requires updating one call site in `_record_frame()`. Without this, I'd have grepped for `write_row` and manually traced the callers. Saved ~2 minutes and gave me confidence the change is contained.

**Call 2: `get_impact("process_frame")`**
Showed 10 direct callers across both live camera and video file paths (5 in main app, 5 in backup). Impact score: 12.0. This confirmed process_frame is a hot path called from multiple code paths in the camera thread — important for understanding that adding computation here affects both live and video modes.

### `get_context` — Best Tool for Understanding a Symbol (4 calls, all high value)

**Most valuable call: `get_context("_record_frame")`**
Returned:
- Full parameter list with docstring
- Every caller (2 sites: `_run_camera_loop:225`, `_run_video_file:339`)
- Every callee: `time.time`, `laser_state_func`, `csv_writer.write_row`, `video_writer.write_frame`, `cv2.cvtColor`, `cv2.circle`, `threshold_writer.write_frame`
- Every `self.` attribute reference (18 refs: `_writer_lock`, `_csv_writer`, `frame_count`, `start_time_ms`, etc.)
- Sibling methods in the same class (14 methods listed with line ranges)
- Diagnostics: none (clean method)

This was like having a pre-built "everything about this method" dossier. It let me design the `FrameMetrics` integration point without reading the full 478-line file first. I could see exactly what state is available, what's called, and where the entry/exit points are.

**`get_context("update_stats")`** — Showed the full method signature, all 25+ callees (including `self._classify_zone`, `self._update_distance_display`, `self._update_velocity_display`), and all sibling methods. This mapped the stats panel's internal structure and confirmed that zone classification logic lives in `_classify_zone` (line 219-265) — which I then knew to extract into the shared `metrics.py`.

### `index` — Fast and Informative (1 call)

Full rebuild took a few seconds. The stats were useful context:
- 512 symbols, 33 classes, 479 functions — small/medium codebase
- 3 DEAD_SYMBOL findings (info), 38 LARGE_SYMBOL warnings
- 0 circular imports — clean architecture

The LARGE_SYMBOL warnings (38) could be useful if surfaced more prominently — they flag methods that might need refactoring.

---

## What Didn't Work

### `search` — Empty Result for Natural Language Query (1 call, low value)

**Query:** `"boundary zone point polygon test classify"`
**Result:** Empty array `[]`

I was looking for the zone classification logic — specifically the `_classify_zone` method in `stats_panel.py` that uses `cv2.pointPolygonTest`. The search returned nothing, even though the codebase has:
- A method literally named `_classify_zone`
- Multiple references to `pointPolygonTest`
- Zone-related variables everywhere

**Expected behavior:** Should have found `_classify_zone` or at least `pointPolygonTest` references.

**Workaround:** Used an Explore agent to read `stats_panel.py` directly.

**Suggestion:** The search seems to require exact symbol name matches. Natural language / keyword matching would be much more useful. Even basic fuzzy matching on `classify` → `_classify_zone` would have worked here.

---

## Tools I Didn't Use (and Why)

### `callers` — Redundant with `get_context`
`get_context` already returns callers as part of its comprehensive output. I never needed `callers` separately. Consider: is there a use case where you want callers but NOT the rest of the context? Maybe for very popular symbols where the full context is too large?

### `file_summary` — Read tool was better
When I needed to understand a file, I needed the actual code, not just a symbol listing. `file_summary` might be useful for a first pass on a totally unfamiliar codebase, but once you're planning changes, you need the code. Could be more useful if it included brief docstrings or the first line of each method.

### `annotate` — No clear trigger
I wasn't sure when to annotate. During a review/planning session, I'm forming mental notes about symbols but the session itself is the artifact. Annotations might be more useful during implementation when you discover something about a symbol that isn't obvious from the code (e.g., "this method is not thread-safe despite appearances").

**Suggestion:** Auto-suggest annotations when the agent discovers something non-obvious? Or prompt at the end of a session: "Would you like to annotate any symbols based on this session?"

### `session` — Didn't think to use it
Could have been useful to track this planning session. I just didn't think of it during the flow. This might benefit from being auto-started when entering plan mode, or suggested when the codeindex is first used in a conversation.

### `diagnostics` — Ran automatically, findings not actionable
The 3 DEAD_SYMBOL and 38 LARGE_SYMBOL findings ran with the index. I didn't investigate them because they weren't related to my task. For a code health review specifically, these would be valuable.

### `check_conventions` — Requires `.codeindex.yaml` config
Didn't have layer definitions set up. This could be very powerful for enforcing the MVVM import rules we defined (e.g., "metrics.py must never import PyQt6").

---

## Recommendations for Improvement

### High Priority

1. **Improve `search` to handle keyword/concept queries**
   - Current: seems to only match exact symbol names
   - Wanted: "zone classification" → finds `_classify_zone`, `_update_zone_display`, `zone_frames`
   - Suggestion: tokenize query, fuzzy-match against symbol names, docstrings, and even code content
   - This was the biggest gap — I had to fall back to Explore agents for conceptual searches

2. **Add a "data flow trace" tool**
   - Input: a variable or dict key (e.g., `result["centroid_x"]`)
   - Output: trace of where it's produced and where it's consumed across files
   - This was the main thing I manually pieced together: `tracking.py` produces `centroid_x` → `camera.py:_record_frame` reads it → `csv_writer.write_row` writes it
   - `get_context` gets partway there (shows callees/callers) but doesn't trace data through dict keys or function arguments

3. **Surface `get_impact` more prominently for change planning**
   - This was the most valuable tool for my use case
   - Suggestion: when an agent is in plan mode and mentions modifying a function, auto-suggest `get_impact` for that function
   - Could also show a "blast radius" summary: "Changing X affects Y files and Z call sites"

### Medium Priority

4. **Auto-start sessions when codeindex is first used**
   - Track which tools were called, what was found, what the agent concluded
   - At session end, generate a review like this one automatically

5. **`file_summary` should include docstrings and key lines**
   - Currently just symbol names/lines
   - Adding first-line docstrings would make it useful as a quick reference without needing full Read

6. **`callers` could be merged into `get_context` as a parameter**
   - `get_context(name, include_callers=True)` vs separate `callers()` tool
   - Reduces tool count, avoids confusion about which to use

### Low Priority

7. **Convention rules for MVVM enforcement**
   - Pre-built rules: "module X should not import module Y"
   - Would catch architectural violations automatically
   - Requires `.codeindex.yaml` setup — maybe auto-generate from CLAUDE.md architecture notes?

8. **Annotation prompting**
   - After a deep exploration session, suggest: "You discovered that _record_frame is the only place CSV data flows through. Want to annotate this?"

---

## Comparison: Codeindex vs. Explore Agents

| Task | Best Tool | Why |
|------|-----------|-----|
| "What calls this function?" | `get_context` | Instant, structured, complete |
| "What breaks if I change this?" | `get_impact` | Pre-computed transitive callers |
| "How does the threshold overlay work?" | Explore agent | Needs to read actual code, understand rendering logic |
| "Find the zone classification logic" | **Should be** `search`, **was** Explore agent | Search failed on keyword query |
| "Understand the full recording pipeline" | Explore agent | Requires reading 4 files, tracing data flow |
| "What's the CSV column format?" | Read tool | Need exact code, not symbol info |

**Bottom line:** Codeindex excels at **structural queries** (who calls what, what's the impact of a change, what are the symbol relationships). It falls short on **semantic/conceptual queries** (find code related to X concept, trace data flow through the system). For a planning session like this one, both types are needed — codeindex handled ~30% of questions excellently, Explore agents handled the rest.

---

## Session Stats

- Total codeindex calls: 8
- Explore agents launched: 3
- Direct Read calls: 6
- Total planning time: ~15 minutes of agent execution
- Outcome: 20-item implementation plan with threading model, test suite, and phased execution order
