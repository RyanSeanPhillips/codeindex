"""
Built-in analysis rules — universal, language-agnostic SQL queries.
"""

from ..store.models import Rule

DEAD_SYMBOL = Rule(
    rule_id="DEAD_SYMBOL",
    name="Dead symbol",
    description="Symbol never referenced in any call site",
    severity="info",
    is_builtin=True,
    sql="""
        SELECT s.symbol_id, s.name, s.kind, s.line_start, f.rel_path, f.file_id
        FROM symbols s
        JOIN files f ON s.file_id = f.file_id
        WHERE s.kind IN ('function', 'method')
          AND s.name NOT LIKE '\\_%' ESCAPE '\\'
          AND s.decorators_json NOT LIKE '%property%'
          AND s.decorators_json NOT LIKE '%.setter%'
          AND s.symbol_id NOT IN (
              SELECT DISTINCT c.caller_id FROM calls c WHERE c.caller_id IS NOT NULL
          )
          AND s.name NOT IN (
              SELECT DISTINCT
                  CASE
                      WHEN INSTR(c.callee_expr, '.') > 0
                      THEN SUBSTR(c.callee_expr, INSTR(c.callee_expr, '.') + 1)
                      ELSE c.callee_expr
                  END
              FROM calls c
          )
    """,
)

LARGE_SYMBOL = Rule(
    rule_id="LARGE_SYMBOL",
    name="Large symbol",
    description="Function/method exceeds 50 lines or complexity > 15",
    severity="warning",
    is_builtin=True,
    sql="""
        SELECT s.symbol_id, s.name, s.kind, s.line_start, s.line_end, s.complexity,
               f.rel_path, f.file_id,
               p.name as parent_name
        FROM symbols s
        JOIN files f ON s.file_id = f.file_id
        LEFT JOIN symbols p ON s.parent_id = p.symbol_id
        WHERE s.kind IN ('function', 'method')
          AND ((s.line_end - s.line_start) > 50 OR s.complexity > 15)
    """,
)

CIRCULAR_IMPORT = Rule(
    rule_id="CIRCULAR_IMPORT",
    name="Circular import",
    description="Module A imports B and B imports A",
    severity="warning",
    is_builtin=True,
    sql="""
        SELECT DISTINCT
            f1.rel_path as file_a, f1.file_id,
            f2.rel_path as file_b,
            i1.module as a_imports
        FROM imports i1
        JOIN files f1 ON i1.file_id = f1.file_id
        JOIN files f2 ON (
            REPLACE(REPLACE(f2.rel_path, '/', '.'), '.py', '') = i1.module
            OR REPLACE(REPLACE(f2.rel_path, '\\', '.'), '.py', '') = i1.module
        )
        JOIN imports i2 ON i2.file_id = f2.file_id
        WHERE (
            REPLACE(REPLACE(f1.rel_path, '/', '.'), '.py', '') = i2.module
            OR REPLACE(REPLACE(f1.rel_path, '\\', '.'), '.py', '') = i2.module
        )
        AND f1.rel_path < f2.rel_path
    """,
)

BUILTIN_RULES = [DEAD_SYMBOL, LARGE_SYMBOL, CIRCULAR_IMPORT]
