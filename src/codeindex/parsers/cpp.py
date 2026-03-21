"""
C++ parser using tree-sitter.

Extracts: classes, structs, functions, methods, namespaces, templates,
#include imports, calls, constructor calls, refs.
"""

from __future__ import annotations

import json
from typing import Optional

from .base import LanguageParser, ParseResult
from ..store.models import Call, Import, Ref, Symbol

# Try tree-sitter; degrade gracefully if unavailable
_TS_AVAILABLE = False
try:
    import tree_sitter_cpp as tscpp
    from tree_sitter import Language, Parser as TSParser

    CPP_LANGUAGE = Language(tscpp.language())
    _TS_AVAILABLE = True
except Exception:
    pass


class CppParser(LanguageParser):
    """C++ source parser. Uses tree-sitter for .cpp/.cxx/.cc/.hpp/.hxx/.hh files."""

    @property
    def language(self) -> str:
        return "cpp"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".cpp", ".cxx", ".cc", ".hpp", ".hxx", ".hh")

    def parse(self, source: str, rel_path: str) -> ParseResult:
        if not _TS_AVAILABLE:
            result = ParseResult()
            result.parse_error = "tree-sitter-cpp not available"
            return result
        return self._parse_tree_sitter(source, rel_path)

    # ── tree-sitter implementation ──

    def _parse_tree_sitter(self, source: str, rel_path: str) -> ParseResult:
        parser = TSParser(CPP_LANGUAGE)
        tree = parser.parse(source.encode("utf-8"))
        result = ParseResult()

        if tree.root_node.has_error:
            result.parse_error = "tree-sitter parse error"

        source_bytes = source.encode("utf-8")
        self._walk_ts(tree.root_node, source_bytes, result, parent_symbol=None,
                      namespace_parts=[])
        return result

    def _walk_ts(self, node, source: bytes, result: ParseResult,
                 parent_symbol: Optional[Symbol], namespace_parts: list[str],
                 depth: int = 0):
        if node.type == "preproc_include":
            self._extract_ts_include(node, source, result)
        elif node.type == "namespace_definition":
            self._walk_namespace(node, source, result, parent_symbol,
                                 namespace_parts, depth)
            return
        elif node.type == "class_specifier":
            sym = self._extract_ts_class(node, source, result, parent_symbol,
                                         namespace_parts)
            if sym:
                for child in node.children:
                    self._walk_ts(child, source, result, parent_symbol=sym,
                                  namespace_parts=namespace_parts, depth=depth + 1)
                return
        elif node.type == "struct_specifier":
            sym = self._extract_ts_struct(node, source, result, parent_symbol,
                                          namespace_parts)
            if sym:
                for child in node.children:
                    self._walk_ts(child, source, result, parent_symbol=sym,
                                  namespace_parts=namespace_parts, depth=depth + 1)
                return
        elif node.type == "template_declaration":
            self._walk_template(node, source, result, parent_symbol,
                                namespace_parts, depth)
            return
        elif node.type == "function_definition":
            sym = self._extract_ts_function(node, source, result, parent_symbol,
                                            namespace_parts)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym,
                              namespace_parts=namespace_parts, depth=depth + 1)
            return
        elif node.type == "call_expression":
            self._extract_ts_call(node, source, result, parent_symbol)
        elif node.type == "new_expression":
            self._extract_ts_new(node, source, result, parent_symbol)
        elif node.type == "field_expression":
            self._extract_ts_ref(node, source, result, parent_symbol)

        for child in node.children:
            self._walk_ts(child, source, result, parent_symbol=parent_symbol,
                          namespace_parts=namespace_parts, depth=depth + 1)

    def _node_text(self, node, source: bytes) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    # ── Namespace handling ──

    def _walk_namespace(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol],
                        namespace_parts: list[str], depth: int):
        """Walk into a namespace_definition, tracking the namespace path."""
        ns_name = ""
        for child in node.children:
            if child.type in ("identifier", "namespace_identifier"):
                ns_name = self._node_text(child, source)

        new_parts = namespace_parts + [ns_name] if ns_name else namespace_parts
        for child in node.children:
            if child.type == "declaration_list":
                for sub in child.children:
                    self._walk_ts(sub, source, result, parent_symbol=parent_symbol,
                                  namespace_parts=new_parts, depth=depth + 1)

    # ── Template handling ──

    def _walk_template(self, node, source: bytes, result: ParseResult,
                       parent_symbol: Optional[Symbol],
                       namespace_parts: list[str], depth: int):
        """Walk a template_declaration, forwarding to the wrapped declaration."""
        for child in node.children:
            if child.type in ("class_specifier", "struct_specifier",
                              "function_definition", "template_declaration"):
                self._walk_ts(child, source, result, parent_symbol=parent_symbol,
                              namespace_parts=namespace_parts, depth=depth + 1)
            elif child.type == "declaration":
                # template function declaration without body
                for sub in child.children:
                    self._walk_ts(sub, source, result, parent_symbol=parent_symbol,
                                  namespace_parts=namespace_parts, depth=depth + 1)

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

    # ── Class extraction ──

    def _extract_ts_class(self, node, source: bytes, result: ParseResult,
                          parent_symbol: Optional[Symbol],
                          namespace_parts: list[str]) -> Optional[Symbol]:
        """Extract a class_specifier as a class symbol."""
        name = ""
        bases = []
        decorators = []

        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)
            elif child.type == "base_class_clause":
                bases = self._extract_bases(child, source, decorators)

        if not name:
            return None

        has_body = any(child.type == "field_declaration_list" for child in node.children)
        if not has_body:
            return None

        if namespace_parts:
            decorators.insert(0, "namespace:" + "::".join(namespace_parts))

        docstring = self._get_preceding_comment(node, source)

        sym = Symbol(
            kind="class",
            name=name,
            bases_json=json.dumps(bases),
            decorators_json=json.dumps(decorators),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    def _extract_bases(self, base_clause_node, source: bytes,
                       decorators: list[str]) -> list[str]:
        """Extract base classes from a base_class_clause node."""
        bases = []
        current_access = None
        for child in base_clause_node.children:
            if child.type == "access_specifier":
                current_access = self._node_text(child, source)
            elif child.type == "type_identifier":
                base_name = self._node_text(child, source)
                bases.append(base_name)
            elif child.type == "qualified_identifier":
                base_name = self._node_text(child, source)
                bases.append(base_name)
            elif child.type == "template_type":
                base_name = self._node_text(child, source)
                bases.append(base_name)
        return bases

    # ── Struct extraction ──

    def _extract_ts_struct(self, node, source: bytes, result: ParseResult,
                           parent_symbol: Optional[Symbol],
                           namespace_parts: list[str]) -> Optional[Symbol]:
        """Extract a struct_specifier as a class symbol."""
        name = ""
        bases = []
        decorators = ["struct"]

        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)
            elif child.type == "base_class_clause":
                bases = self._extract_bases(child, source, decorators)

        if not name:
            return None

        has_body = any(child.type == "field_declaration_list" for child in node.children)
        if not has_body:
            return None

        if namespace_parts:
            decorators.insert(0, "namespace:" + "::".join(namespace_parts))

        docstring = self._get_preceding_comment(node, source)

        sym = Symbol(
            kind="class",
            name=name,
            bases_json=json.dumps(bases),
            decorators_json=json.dumps(decorators),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    # ── Function extraction ──

    def _extract_ts_function(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Optional[Symbol],
                             namespace_parts: list[str]) -> Symbol:
        """Extract a function_definition."""
        name = ""
        params = []
        return_type = None
        decorators = []
        is_virtual = False

        for child in node.children:
            if child.type == "function_declarator":
                name, params = self._parse_function_declarator(child, source)
            elif child.type == "pointer_declarator":
                for subchild in child.children:
                    if subchild.type == "function_declarator":
                        name, params = self._parse_function_declarator(subchild, source)
                        break
            elif child.type in ("primitive_type", "sized_type_specifier",
                                "type_identifier", "qualified_identifier",
                                "template_type", "auto"):
                return_type = self._node_text(child, source)
            elif child.type == "virtual_function_specifier" or \
                 child.type == "virtual":
                is_virtual = True
            elif child.type == "storage_class_specifier":
                spec = self._node_text(child, source)
                if spec == "static":
                    decorators.append("static")

        # If direct children didn't give us a name, search deeper
        if not name:
            for child in node.children:
                if child.type.endswith("_declarator"):
                    name, params = self._find_func_declarator(child, source)
                    if name:
                        break

        if is_virtual:
            decorators.append("virtual")

        # Detect constructor/destructor by matching class name
        is_constructor = False
        is_destructor = False
        if parent_symbol and parent_symbol.kind == "class":
            if name == parent_symbol.name:
                is_constructor = True
                decorators.append("constructor")
            elif name == f"~{parent_symbol.name}":
                is_destructor = True
                decorators.append("destructor")

        if namespace_parts and not parent_symbol:
            decorators.insert(0, "namespace:" + "::".join(namespace_parts))

        # Handle qualified names (e.g. ClassName::method_name)
        if "::" in name and not parent_symbol:
            parts = name.rsplit("::", 1)
            name = parts[-1]
            decorators.append(f"qualified:{parts[0]}")

        docstring = self._get_preceding_comment(node, source)
        complexity = self._compute_complexity(node)

        kind = "method" if parent_symbol and parent_symbol.kind == "class" else "function"

        sym = Symbol(
            kind=kind,
            name=name,
            params_json=json.dumps(params),
            return_type=return_type,
            decorators_json=json.dumps(decorators),
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
            elif child.type == "qualified_identifier":
                name = self._node_text(child, source)
            elif child.type == "field_identifier":
                name = self._node_text(child, source)
            elif child.type == "destructor_name":
                name = self._node_text(child, source)
            elif child.type == "operator_name":
                name = self._node_text(child, source)
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
            elif child.type == "optional_parameter_declaration":
                param = self._parse_optional_param(child, source)
                if param:
                    params.append(param)
            elif child.type == "variadic_parameter_declaration":
                params.append({"name": "...", "type": "..."})
        return params

    def _parse_param_declaration(self, node, source: bytes) -> Optional[dict]:
        """Parse a single parameter_declaration node."""
        type_parts = []
        name = ""
        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type in ("pointer_declarator", "reference_declarator",
                                "array_declarator"):
                for subchild in self._walk_all(child):
                    if subchild.type == "identifier":
                        name = self._node_text(subchild, source)
                        break
                type_parts.append(self._node_text(child, source))
            elif child.type in ("primitive_type", "sized_type_specifier",
                                "type_identifier", "qualified_identifier",
                                "template_type", "type_qualifier", "auto"):
                type_parts.append(self._node_text(child, source))

        full_text = self._node_text(node, source).strip()
        if full_text == "void":
            return None

        type_str = " ".join(type_parts) if type_parts else None
        result = {"name": name if name else full_text}
        if type_str and name:
            result["type"] = type_str
        return result

    def _parse_optional_param(self, node, source: bytes) -> Optional[dict]:
        """Parse a parameter with a default value."""
        param = self._parse_param_declaration(node, source)
        if param is None:
            return None

        # Find the default value (everything after '=')
        found_eq = False
        for child in node.children:
            if child.type == "=":
                found_eq = True
            elif found_eq:
                param["default"] = self._node_text(child, source)
                break
        return param

    # ── Call extraction ──

    def _extract_ts_call(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]):
        """Extract a function call expression."""
        func_node = None
        for child in node.children:
            if child.type in ("identifier", "qualified_identifier",
                              "field_expression", "template_function",
                              "scoped_identifier"):
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

    def _extract_ts_new(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol]):
        """Extract a new expression as a constructor call."""
        type_name = ""
        for child in node.children:
            if child.type in ("type_identifier", "qualified_identifier",
                              "template_type", "scoped_identifier"):
                type_name = self._node_text(child, source)
                break

        if not type_name:
            return

        call = Call(
            callee_expr=type_name,
            line_no=node.start_point[0] + 1,
        )
        call._pending_caller = parent_symbol
        result.calls.append(call)

    # ── Reference extraction ──

    def _extract_ts_ref(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol]):
        """Extract field_expression (e.g. obj.field, obj->field, this->field) as ref."""
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

        # Take only the first member for nested access
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
                              "do_statement", "case_statement",
                              "for_range_loop"):
                complexity += 1
            elif child.type == "conditional_expression":
                complexity += 1
            elif child.type in ("&&", "||"):
                complexity += 1
            elif child.type == "catch_clause":
                complexity += 1
        return complexity

    def _walk_all(self, node):
        """Yield all descendant nodes."""
        for child in node.children:
            yield child
            yield from self._walk_all(child)
