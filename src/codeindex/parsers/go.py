"""
Go parser using tree-sitter.

Extracts: functions, methods, structs, interfaces, calls, imports, attribute refs.
"""

from __future__ import annotations

import json
from typing import Optional

from .base import LanguageParser, ParseResult
from ..store.models import Call, Import, Ref, Symbol

# Try tree-sitter; gracefully degrade if not installed
_TS_AVAILABLE = False
try:
    import tree_sitter_go as tsgo
    from tree_sitter import Language, Parser as TSParser

    GO_LANGUAGE = Language(tsgo.language())
    _TS_AVAILABLE = True
except Exception:
    pass


class GoParser(LanguageParser):
    """Go source parser. Requires tree-sitter-go."""

    @property
    def language(self) -> str:
        return "go"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".go",)

    def parse(self, source: str, rel_path: str) -> ParseResult:
        if not _TS_AVAILABLE:
            result = ParseResult()
            result.parse_error = "tree-sitter-go not available"
            return result
        return self._parse_tree_sitter(source, rel_path)

    # ── tree-sitter implementation ──

    def _parse_tree_sitter(self, source: str, rel_path: str) -> ParseResult:
        parser = TSParser(GO_LANGUAGE)
        tree = parser.parse(source.encode("utf-8"))
        result = ParseResult()

        if tree.root_node.has_error:
            result.parse_error = "tree-sitter parse error"

        source_bytes = source.encode("utf-8")
        self._walk_ts(tree.root_node, source_bytes, result, parent_symbol=None)
        return result

    def _walk_ts(self, node, source: bytes, result: ParseResult,
                 parent_symbol: Optional[Symbol], depth: int = 0):
        if node.type == "import_declaration":
            self._extract_ts_import(node, source, result)
        elif node.type == "function_declaration":
            sym = self._extract_ts_function(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "method_declaration":
            sym = self._extract_ts_method(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "type_declaration":
            self._extract_ts_type_declaration(node, source, result, parent_symbol)
            return
        elif node.type == "call_expression":
            self._extract_ts_call(node, source, result, parent_symbol)
        elif node.type == "selector_expression":
            self._extract_ts_ref(node, source, result, parent_symbol)
        elif node.type == "composite_literal":
            self._extract_ts_composite_literal(node, source, result, parent_symbol)

        for child in node.children:
            self._walk_ts(child, source, result, parent_symbol=parent_symbol, depth=depth + 1)

    def _node_text(self, node, source: bytes) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    # ── import extraction ──

    def _extract_ts_import(self, node, source: bytes, result: ParseResult):
        """Extract Go imports: import "fmt" or import ( "fmt"; "os" )."""
        for child in node.children:
            if child.type == "import_spec":
                self._extract_import_spec(child, source, result)
            elif child.type == "import_spec_list":
                for spec in child.children:
                    if spec.type == "import_spec":
                        self._extract_import_spec(spec, source, result)

    def _extract_import_spec(self, node, source: bytes, result: ParseResult):
        """Extract a single import spec: name "path" or just "path"."""
        alias = None
        module = ""
        for child in node.children:
            if child.type == "package_identifier":
                alias = self._node_text(child, source)
            elif child.type == "interpreted_string_literal":
                module = self._node_text(child, source).strip('"')
            elif child.type == "dot":
                alias = "."
            elif child.type == "blank_identifier":
                alias = "_"
        result.imports.append(Import(
            module=module,
            alias=alias,
            is_from=False,
            line_no=node.start_point[0] + 1,
        ))

    # ── function extraction ──

    def _extract_ts_function(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract a top-level function declaration."""
        name = ""
        params = []
        return_type = None

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "parameter_list":
                params = self._extract_ts_params(child, source)
            elif child.type in ("type_identifier", "qualified_type",
                                "pointer_type", "slice_type", "map_type",
                                "array_type", "channel_type", "interface_type",
                                "struct_type", "function_type"):
                return_type = self._node_text(child, source)
            elif child.type == "parameter_list" and return_type is None:
                # Could be a result parameter list: (int, error)
                pass

        # Handle multiple return types in parenthesized result
        if return_type is None:
            for child in node.children:
                if child.type == "parameter_list":
                    # Second parameter_list is the result
                    candidate = self._node_text(child, source)
                    if candidate != self._format_params_text(params):
                        return_type = candidate
                        break

        docstring = self._get_ts_comment(node, source)
        complexity = self._compute_ts_complexity(node)

        # Detect goroutine launch: `go func()` — the `go` keyword precedes
        is_async = False

        sym = Symbol(
            kind="function",
            name=name,
            params_json=json.dumps(params),
            return_type=return_type,
            decorators_json=json.dumps([]),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            complexity=complexity,
            is_async=is_async,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    def _extract_ts_method(self, node, source: bytes, result: ParseResult,
                           parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract a method declaration: func (r *Receiver) Name(params) result."""
        name = ""
        params = []
        return_type = None
        receiver_type = None

        param_list_count = 0
        for child in node.children:
            if child.type == "field_identifier":
                name = self._node_text(child, source)
            elif child.type == "parameter_list":
                param_list_count += 1
                if param_list_count == 1:
                    # First parameter_list is the receiver
                    receiver_type = self._extract_receiver_type(child, source)
                elif param_list_count == 2:
                    # Second parameter_list is the actual params
                    params = self._extract_ts_params(child, source)
                else:
                    # Third parameter_list is the result list
                    return_type = self._node_text(child, source)
            elif child.type in ("type_identifier", "qualified_type",
                                "pointer_type", "slice_type", "map_type",
                                "array_type", "channel_type", "interface_type",
                                "struct_type", "function_type"):
                return_type = self._node_text(child, source)

        docstring = self._get_ts_comment(node, source)
        complexity = self._compute_ts_complexity(node)

        # Create a virtual parent symbol for the receiver type so indexer can link
        receiver_sym = None
        if receiver_type:
            # Look for an existing struct symbol in result
            for s in result.symbols:
                if s.kind in ("class", "interface") and s.name == receiver_type:
                    receiver_sym = s
                    break

        sym = Symbol(
            kind="method",
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
        sym._pending_parent = receiver_sym if receiver_sym else parent_symbol
        # Stash receiver type name so the indexer can resolve it later
        sym._receiver_type = receiver_type
        result.symbols.append(sym)
        return sym

    def _extract_receiver_type(self, param_list_node, source: bytes) -> Optional[str]:
        """Extract the receiver type from a method's first parameter_list."""
        for child in param_list_node.children:
            if child.type == "parameter_declaration":
                for part in child.children:
                    if part.type == "type_identifier":
                        return self._node_text(part, source)
                    elif part.type == "pointer_type":
                        for inner in part.children:
                            if inner.type == "type_identifier":
                                return self._node_text(inner, source)
        return None

    def _format_params_text(self, params: list[dict]) -> str:
        """Build a rough text representation of params for comparison."""
        parts = []
        for p in params:
            s = p.get("name", "")
            if "type" in p:
                s += " " + p["type"]
            parts.append(s)
        return "(" + ", ".join(parts) + ")"

    # ── type declarations (struct, interface) ──

    def _extract_ts_type_declaration(self, node, source: bytes, result: ParseResult,
                                     parent_symbol: Optional[Symbol]):
        """Extract type declarations: type Foo struct{} or type Bar interface{}."""
        for child in node.children:
            if child.type == "type_spec":
                self._extract_ts_type_spec(child, source, result, parent_symbol)

    def _extract_ts_type_spec(self, node, source: bytes, result: ParseResult,
                              parent_symbol: Optional[Symbol]):
        """Process a single type_spec inside a type_declaration."""
        name = ""
        kind = "class"
        bases = []

        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)
            elif child.type == "struct_type":
                kind = "class"
                bases = self._extract_struct_embedded(child, source)
            elif child.type == "interface_type":
                kind = "interface"
                bases = self._extract_interface_embedded(child, source)
            elif child.type in ("type_identifier", "qualified_type"):
                # Type alias: type Foo = Bar
                pass

        docstring = self._get_ts_comment(node, source)

        sym = Symbol(
            kind=kind,
            name=name,
            bases_json=json.dumps(bases),
            decorators_json=json.dumps([]),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)

        # Walk children for interface method signatures
        if kind == "interface":
            self._extract_interface_methods(node, source, result, sym)

    def _extract_struct_embedded(self, node, source: bytes) -> list[str]:
        """Extract embedded types from a struct body (composition)."""
        bases = []
        for child in node.children:
            if child.type == "field_declaration_list":
                for field_decl in child.children:
                    if field_decl.type == "field_declaration":
                        # An embedded field has no field name, just a type
                        children = [c for c in field_decl.children
                                    if c.type not in ("comment", ",")]
                        if len(children) == 1:
                            type_text = self._node_text(children[0], source)
                            type_text = type_text.lstrip("*")
                            bases.append(type_text)
        return bases

    def _extract_interface_embedded(self, node, source: bytes) -> list[str]:
        """Extract embedded interfaces from an interface body."""
        bases = []
        for child in node.children:
            if child.type == "type_identifier":
                bases.append(self._node_text(child, source))
            elif child.type == "qualified_type":
                bases.append(self._node_text(child, source))
        return bases

    def _extract_interface_methods(self, type_spec_node, source: bytes,
                                   result: ParseResult, parent_symbol: Symbol):
        """Extract method signatures from an interface definition."""
        for child in type_spec_node.children:
            if child.type == "interface_type":
                for member in child.children:
                    if member.type == "method_spec":
                        self._extract_method_spec(member, source, result, parent_symbol)

    def _extract_method_spec(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Symbol):
        """Extract a method signature from an interface method_spec."""
        name = ""
        params = []
        return_type = None
        param_list_count = 0

        for child in node.children:
            if child.type == "field_identifier":
                name = self._node_text(child, source)
            elif child.type == "parameter_list":
                param_list_count += 1
                if param_list_count == 1:
                    params = self._extract_ts_params(child, source)
                else:
                    return_type = self._node_text(child, source)
            elif child.type in ("type_identifier", "qualified_type",
                                "pointer_type", "slice_type", "map_type"):
                return_type = self._node_text(child, source)

        sym = Symbol(
            kind="method",
            name=name,
            params_json=json.dumps(params),
            return_type=return_type,
            decorators_json=json.dumps([]),
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)

    # ── parameter extraction ──

    def _extract_ts_params(self, node, source: bytes) -> list[dict]:
        """Extract parameters from a parameter_list node."""
        params = []
        for child in node.children:
            if child.type == "parameter_declaration":
                params.extend(self._parse_ts_param_decl(child, source))
            elif child.type == "variadic_parameter_declaration":
                params.extend(self._parse_ts_variadic_param(child, source))
        return params

    def _parse_ts_param_decl(self, node, source: bytes) -> list[dict]:
        """Parse a parameter_declaration: name type or name1, name2 type."""
        names = []
        type_str = None
        for child in node.children:
            if child.type == "identifier":
                names.append(self._node_text(child, source))
            elif child.type not in (",", "(", ")"):
                type_str = self._node_text(child, source)
        result = []
        for name in names:
            p: dict = {"name": name}
            if type_str:
                p["type"] = type_str
            result.append(p)
        # If no names but there is a type (unnamed params), still record
        if not names and type_str:
            result.append({"name": "_", "type": type_str})
        return result

    def _parse_ts_variadic_param(self, node, source: bytes) -> list[dict]:
        """Parse a variadic_parameter_declaration: name ...type."""
        name = ""
        type_str = None
        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type not in ("...", ",", "(", ")"):
                type_str = self._node_text(child, source)
        p: dict = {"name": "..." + name if name else "..."}
        if type_str:
            p["type"] = type_str
        return [p]

    # ── call extraction ──

    def _extract_ts_call(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]):
        """Extract a call expression."""
        func_node = node.children[0] if node.children else None
        if not func_node:
            return
        callee = self._node_text(func_node, source)

        call = Call(
            callee_expr=callee,
            line_no=node.start_point[0] + 1,
        )
        call._pending_caller = parent_symbol
        result.calls.append(call)

    # ── composite literal (constructor-like) ──

    def _extract_ts_composite_literal(self, node, source: bytes, result: ParseResult,
                                      parent_symbol: Optional[Symbol]):
        """Extract composite literal as a constructor-like call: MyStruct{...}."""
        type_node = node.children[0] if node.children else None
        if not type_node:
            return
        if type_node.type in ("type_identifier", "qualified_type",
                              "selector_expression"):
            callee = self._node_text(type_node, source)
            call = Call(
                callee_expr=callee,
                line_no=node.start_point[0] + 1,
            )
            call._pending_caller = parent_symbol
            result.calls.append(call)

    # ── reference extraction ──

    def _extract_ts_ref(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol]):
        """Extract selector expression (pkg.Name or obj.Field) as a ref."""
        full_text = self._node_text(node, source)
        parts = full_text.split(".")
        if len(parts) < 2:
            return

        target = parts[0]
        name = parts[1]

        ref = Ref(
            ref_kind="read",
            target=target,
            name=name,
            line_no=node.start_point[0] + 1,
        )
        ref._pending_symbol = parent_symbol
        result.refs.append(ref)

    # ── docstring / comment extraction ──

    def _get_ts_comment(self, node, source: bytes) -> Optional[str]:
        """Extract the comment block preceding a node (Go convention: // comments)."""
        comments = []
        prev = node.prev_named_sibling
        while prev and prev.type == "comment":
            text = self._node_text(prev, source)
            # Strip // prefix
            if text.startswith("//"):
                text = text[2:].strip()
            comments.insert(0, text)
            prev = prev.prev_named_sibling

        if comments:
            return "\n".join(comments)
        return None

    # ── cyclomatic complexity ──

    def _compute_ts_complexity(self, node) -> int:
        """Compute cyclomatic complexity for a function/method body."""
        complexity = 1
        for child in self._walk_all(node):
            if child.type in ("if_statement", "for_statement",
                              "expression_switch_statement",
                              "type_switch_statement",
                              "select_statement"):
                complexity += 1
            elif child.type in ("expression_case", "type_case", "default_case",
                                "communication_case"):
                complexity += 1
            elif child.type in ("binary_expression",):
                op = self._get_binary_op(child)
                if op in ("&&", "||"):
                    complexity += 1
        return complexity

    def _get_binary_op(self, node) -> Optional[str]:
        """Get the operator from a binary expression."""
        for child in node.children:
            if child.type in ("&&", "||"):
                return child.type
        return None

    def _walk_all(self, node):
        """Recursively yield all descendant nodes."""
        for child in node.children:
            yield child
            yield from self._walk_all(child)
