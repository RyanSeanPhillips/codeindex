"""
C parser using tree-sitter.

Extracts: functions, structs, enums, typedefs, #include imports, calls, refs.
"""

from __future__ import annotations

import json
from typing import Optional

from .base import LanguageParser, ParseResult
from ..store.models import Call, Import, Ref, Symbol

# Try tree-sitter; degrade gracefully if unavailable
_TS_AVAILABLE = False
try:
    import tree_sitter_c as tsc
    from tree_sitter import Language, Parser as TSParser

    C_LANGUAGE = Language(tsc.language())
    _TS_AVAILABLE = True
except Exception:
    pass


class CParser(LanguageParser):
    """C source parser. Uses tree-sitter for .c and .h files."""

    @property
    def language(self) -> str:
        return "c"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".c", ".h")

    def parse(self, source: str, rel_path: str) -> ParseResult:
        if not _TS_AVAILABLE:
            result = ParseResult()
            result.parse_error = "tree-sitter-c not available"
            return result
        return self._parse_tree_sitter(source, rel_path)

    # ── tree-sitter implementation ──

    def _parse_tree_sitter(self, source: str, rel_path: str) -> ParseResult:
        parser = TSParser(C_LANGUAGE)
        tree = parser.parse(source.encode("utf-8"))
        result = ParseResult()

        if tree.root_node.has_error:
            result.parse_error = "tree-sitter parse error"

        source_bytes = source.encode("utf-8")
        self._walk_ts(tree.root_node, source_bytes, result, parent_symbol=None)
        return result

    def _walk_ts(self, node, source: bytes, result: ParseResult,
                 parent_symbol: Optional[Symbol], depth: int = 0):
        if node.type == "preproc_include":
            self._extract_ts_include(node, source, result)
        elif node.type == "function_definition":
            sym = self._extract_ts_function(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "struct_specifier":
            sym = self._extract_ts_struct(node, source, result, parent_symbol)
            if sym:
                for child in node.children:
                    self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
                return
        elif node.type == "enum_specifier":
            self._extract_ts_enum(node, source, result, parent_symbol)
        elif node.type == "type_definition":
            self._extract_ts_typedef(node, source, result, parent_symbol)
        elif node.type == "call_expression":
            self._extract_ts_call(node, source, result, parent_symbol)
        elif node.type == "field_expression":
            self._extract_ts_ref(node, source, result, parent_symbol)

        for child in node.children:
            self._walk_ts(child, source, result, parent_symbol=parent_symbol, depth=depth + 1)

    def _node_text(self, node, source: bytes) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    # ── Include extraction ──

    def _extract_ts_include(self, node, source: bytes, result: ParseResult):
        """Extract #include directives as imports."""
        path_node = None
        for child in node.children:
            if child.type in ("system_lib_string", "string_literal"):
                path_node = child
                break
        if path_node is None:
            return

        raw = self._node_text(path_node, source)
        # Strip surrounding <> or ""
        if (raw.startswith("<") and raw.endswith(">")) or \
           (raw.startswith('"') and raw.endswith('"')):
            module = raw[1:-1]
        else:
            module = raw

        is_system = path_node.type == "system_lib_string"
        result.imports.append(Import(
            module=module,
            is_from=is_system,
            line_no=node.start_point[0] + 1,
        ))

    # ── Function extraction ──

    def _extract_ts_function(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract a function definition."""
        name = ""
        params = []
        return_type = None

        # The structure is: type declarator compound_statement
        # The declarator contains the function_declarator with name and params
        for child in node.children:
            if child.type == "function_declarator":
                name, params = self._parse_function_declarator(child, source)
            elif child.type == "pointer_declarator":
                # e.g. *func_name(...)
                for subchild in child.children:
                    if subchild.type == "function_declarator":
                        name, params = self._parse_function_declarator(subchild, source)
                        break
            elif child.type in ("primitive_type", "sized_type_specifier",
                                "type_identifier", "struct_specifier",
                                "enum_specifier"):
                return_type = self._node_text(child, source)
            elif child.type == "storage_class_specifier":
                pass  # static, extern, etc.
            elif child.type == "type_qualifier":
                pass  # const, volatile, etc.

        # If we didn't find a function_declarator as a direct child, the
        # declarator node wraps it
        if not name:
            for child in node.children:
                if child.type == "declarator" or child.type.endswith("_declarator"):
                    name, params = self._find_func_declarator(child, source)
                    if name:
                        break

        docstring = self._get_preceding_comment(node, source)
        complexity = self._compute_complexity(node)

        kind = "method" if parent_symbol and parent_symbol.kind == "class" else "function"

        sym = Symbol(
            kind=kind,
            name=name,
            params_json=json.dumps(params),
            return_type=return_type,
            decorators_json=json.dumps([]),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            complexity=complexity,
            is_async=False,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    def _find_func_declarator(self, node, source: bytes) -> tuple[str, list[dict]]:
        """Recursively find a function_declarator inside declarator wrappers."""
        if node.type == "function_declarator":
            return self._parse_function_declarator(node, source)
        for child in node.children:
            name, params = self._find_func_declarator(child, source)
            if name:
                return name, params
        return "", []

    def _parse_function_declarator(self, node, source: bytes) -> tuple[str, list[dict]]:
        """Parse function_declarator to get name and parameter list."""
        name = ""
        params = []
        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "parenthesized_declarator":
                # e.g. (*func_ptr)(...)
                for subchild in child.children:
                    if subchild.type == "pointer_declarator":
                        for inner in subchild.children:
                            if inner.type == "identifier":
                                name = self._node_text(inner, source)
            elif child.type == "parameter_list":
                params = self._extract_params(child, source)
        return name, params

    def _extract_params(self, node, source: bytes) -> list[dict]:
        """Extract function parameters from a parameter_list node."""
        params = []
        for child in node.children:
            if child.type == "parameter_declaration":
                param = self._parse_param_declaration(child, source)
                if param:
                    params.append(param)
            elif child.type == "variadic_parameter":
                params.append({"name": "...", "type": "..."})
        return params

    def _parse_param_declaration(self, node, source: bytes) -> Optional[dict]:
        """Parse a single parameter_declaration node."""
        type_parts = []
        name = ""
        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type in ("pointer_declarator", "array_declarator"):
                # Extract name from nested declarator
                for subchild in self._walk_all(child):
                    if subchild.type == "identifier":
                        name = self._node_text(subchild, source)
                        break
                type_parts.append(self._node_text(child, source))
            elif child.type in ("primitive_type", "sized_type_specifier",
                                "type_identifier", "struct_specifier",
                                "enum_specifier", "type_qualifier"):
                type_parts.append(self._node_text(child, source))

        # Handle void parameter (void alone means no params)
        full_text = self._node_text(node, source).strip()
        if full_text == "void":
            return None

        type_str = " ".join(type_parts) if type_parts else None
        result = {"name": name if name else full_text}
        if type_str and name:
            result["type"] = type_str
        return result

    # ── Struct extraction ──

    def _extract_ts_struct(self, node, source: bytes, result: ParseResult,
                           parent_symbol: Optional[Symbol]) -> Optional[Symbol]:
        """Extract a struct definition as a class-kind symbol."""
        name = ""
        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)

        if not name:
            return None

        # Only treat as a symbol if there is a body (field_declaration_list)
        has_body = any(child.type == "field_declaration_list" for child in node.children)
        if not has_body:
            return None

        docstring = self._get_preceding_comment(node, source)

        sym = Symbol(
            kind="class",
            name=name,
            decorators_json=json.dumps(["struct"]),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    # ── Enum extraction ──

    def _extract_ts_enum(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]):
        """Extract an enum definition as a class-kind symbol."""
        name = ""
        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)

        if not name:
            return

        has_body = any(child.type == "enumerator_list" for child in node.children)
        if not has_body:
            return

        docstring = self._get_preceding_comment(node, source)

        sym = Symbol(
            kind="class",
            name=name,
            decorators_json=json.dumps(["enum"]),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)

    # ── Typedef extraction ──

    def _extract_ts_typedef(self, node, source: bytes, result: ParseResult,
                            parent_symbol: Optional[Symbol]):
        """Extract a typedef as a class-kind symbol."""
        # typedef struct { ... } Name;
        # The last identifier before ';' is the typedef name
        name = ""
        type_declarator = None
        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)
            elif child.type in ("struct_specifier", "enum_specifier"):
                type_declarator = child

        if not name:
            return

        # If the typedef wraps a struct/enum with a body, we already extracted
        # that struct/enum in the walk. Just record the typedef alias.
        decorators = ["typedef"]
        if type_declarator:
            if type_declarator.type == "struct_specifier":
                decorators.append("struct")
            elif type_declarator.type == "enum_specifier":
                decorators.append("enum")

        docstring = self._get_preceding_comment(node, source)

        sym = Symbol(
            kind="class",
            name=name,
            decorators_json=json.dumps(decorators),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)

    # ── Call extraction ──

    def _extract_ts_call(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]):
        """Extract a function call expression."""
        func_node = None
        for child in node.children:
            if child.type == "identifier":
                func_node = child
                break
            elif child.type == "field_expression":
                func_node = child
                break
            elif child.type == "parenthesized_expression":
                func_node = child
                break

        if func_node is None:
            return

        callee = self._node_text(func_node, source)
        call = Call(
            callee_expr=callee,
            line_no=node.start_point[0] + 1,
        )
        call._pending_caller = parent_symbol
        result.calls.append(call)

    # ── Reference extraction ──

    def _extract_ts_ref(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol]):
        """Extract field_expression (e.g. obj->field or obj.field) as a ref."""
        full_text = self._node_text(node, source)

        # Split on -> and .
        if "->" in full_text:
            parts = full_text.split("->", 1)
        elif "." in full_text:
            parts = full_text.split(".", 1)
        else:
            return

        if len(parts) != 2:
            return

        target = parts[0].strip()
        name = parts[1].strip()

        # Skip if name contains further derefs (nested access handled elsewhere)
        if "->" in name or "." in name:
            # Take only the first member
            for sep in ("->", "."):
                if sep in name:
                    name = name.split(sep, 1)[0].strip()
                    break

        ref = Ref(
            ref_kind="read",
            target=target,
            name=name,
            line_no=node.start_point[0] + 1,
        )
        ref._pending_symbol = parent_symbol
        result.refs.append(ref)

    # ── Comment / docstring extraction ──

    def _get_preceding_comment(self, node, source: bytes) -> Optional[str]:
        """Get the comment block immediately preceding a node."""
        prev = node.prev_named_sibling
        if prev is None:
            return None

        comments = []
        while prev and prev.type == "comment":
            comments.insert(0, self._node_text(prev, source))
            prev = prev.prev_named_sibling

        if not comments:
            return None

        lines = []
        for c in comments:
            text = c.strip()
            if text.startswith("/*") and text.endswith("*/"):
                # Block comment
                text = text[2:-2].strip()
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("*"):
                        line = line[1:].strip()
                    lines.append(line)
            elif text.startswith("//"):
                lines.append(text[2:].strip())

        return "\n".join(lines).strip() if lines else None

    # ── Complexity computation ──

    def _compute_complexity(self, node) -> int:
        """Compute cyclomatic complexity for a function."""
        complexity = 1
        for child in self._walk_all(node):
            if child.type in ("if_statement", "for_statement", "while_statement",
                              "do_statement", "case_statement"):
                complexity += 1
            elif child.type == "conditional_expression":
                complexity += 1  # ternary operator
            elif child.type in ("&&", "||"):
                complexity += 1
        return complexity

    def _walk_all(self, node):
        """Yield all descendant nodes."""
        for child in node.children:
            yield child
            yield from self._walk_all(child)
