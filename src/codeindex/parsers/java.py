"""
Java parser using tree-sitter.

Extracts: classes, interfaces, enums, methods, constructors, calls,
imports, annotations, attribute refs.
"""

from __future__ import annotations

import json
from typing import Optional

from .base import LanguageParser, ParseResult
from ..store.models import Call, Import, Ref, Symbol

# Try tree-sitter; gracefully degrade if not installed
_TS_AVAILABLE = False
try:
    import tree_sitter_java as tsjava
    from tree_sitter import Language, Parser as TSParser

    JAVA_LANGUAGE = Language(tsjava.language())
    _TS_AVAILABLE = True
except Exception:
    pass


class JavaParser(LanguageParser):
    """Java source parser. Requires tree-sitter-java."""

    @property
    def language(self) -> str:
        return "java"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".java",)

    def parse(self, source: str, rel_path: str) -> ParseResult:
        if not _TS_AVAILABLE:
            result = ParseResult()
            result.parse_error = "tree-sitter-java not available"
            return result
        return self._parse_tree_sitter(source, rel_path)

    # ── tree-sitter implementation ──

    def _parse_tree_sitter(self, source: str, rel_path: str) -> ParseResult:
        parser = TSParser(JAVA_LANGUAGE)
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
        elif node.type == "class_declaration":
            sym = self._extract_ts_class(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "interface_declaration":
            sym = self._extract_ts_interface(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "enum_declaration":
            sym = self._extract_ts_enum(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "method_declaration":
            sym = self._extract_ts_method(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "constructor_declaration":
            sym = self._extract_ts_constructor(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "method_invocation":
            self._extract_ts_method_call(node, source, result, parent_symbol)
        elif node.type == "object_creation_expression":
            self._extract_ts_new_call(node, source, result, parent_symbol)
        elif node.type == "field_access":
            self._extract_ts_ref(node, source, result, parent_symbol)

        for child in node.children:
            self._walk_ts(child, source, result, parent_symbol=parent_symbol, depth=depth + 1)

    def _node_text(self, node, source: bytes) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    # ── import extraction ──

    def _extract_ts_import(self, node, source: bytes, result: ParseResult):
        """Extract import declaration: import java.util.List; import static ...;"""
        is_static = False
        module_text = ""

        for child in node.children:
            if child.type == "static":
                is_static = True
            elif child.type == "scoped_identifier":
                module_text = self._node_text(child, source)
            elif child.type == "scoped_absolute_identifier":
                module_text = self._node_text(child, source)
            elif child.type == "identifier":
                module_text = self._node_text(child, source)

        # Split into module and name: java.util.List → module=java.util, name=List
        parts = module_text.rsplit(".", 1)
        if len(parts) == 2:
            module = parts[0]
            name = parts[1]
            alias = None
            if is_static:
                alias = "static"
            result.imports.append(Import(
                module=module,
                name=name,
                alias=alias,
                is_from=True,
                line_no=node.start_point[0] + 1,
            ))
        else:
            result.imports.append(Import(
                module=module_text,
                is_from=False,
                line_no=node.start_point[0] + 1,
            ))

    # ── class extraction ──

    def _extract_ts_class(self, node, source: bytes, result: ParseResult,
                          parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract class_declaration: class Foo extends Bar implements Baz { ... }."""
        name = ""
        bases = []
        decorators = []

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "modifiers":
                decorators = self._extract_modifiers(child, source)
            elif child.type == "superclass":
                for sc_child in child.children:
                    if sc_child.type in ("type_identifier", "generic_type",
                                         "scoped_type_identifier"):
                        bases.append(self._node_text(sc_child, source))
            elif child.type == "super_interfaces":
                for si_child in child.children:
                    if si_child.type == "type_list":
                        for t in si_child.children:
                            if t.type in ("type_identifier", "generic_type",
                                          "scoped_type_identifier"):
                                bases.append(self._node_text(t, source))
            elif child.type == "type_parameters":
                # Generics: class Foo<T> → append to name for clarity
                name += self._node_text(child, source)

        docstring = self._get_ts_javadoc(node, source)

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

    # ── interface extraction ──

    def _extract_ts_interface(self, node, source: bytes, result: ParseResult,
                              parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract interface_declaration: interface Foo extends Bar { ... }."""
        name = ""
        bases = []
        decorators = []

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "modifiers":
                decorators = self._extract_modifiers(child, source)
            elif child.type == "extends_interfaces":
                for ei_child in child.children:
                    if ei_child.type == "type_list":
                        for t in ei_child.children:
                            if t.type in ("type_identifier", "generic_type",
                                          "scoped_type_identifier"):
                                bases.append(self._node_text(t, source))
            elif child.type == "type_parameters":
                name += self._node_text(child, source)

        docstring = self._get_ts_javadoc(node, source)

        sym = Symbol(
            kind="interface",
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

    # ── enum extraction ──

    def _extract_ts_enum(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract enum_declaration: enum Color { RED, GREEN, BLUE }."""
        name = ""
        bases = []
        decorators = []

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "modifiers":
                decorators = self._extract_modifiers(child, source)
            elif child.type == "super_interfaces":
                for si_child in child.children:
                    if si_child.type == "type_list":
                        for t in si_child.children:
                            if t.type in ("type_identifier", "generic_type",
                                          "scoped_type_identifier"):
                                bases.append(self._node_text(t, source))

        docstring = self._get_ts_javadoc(node, source)

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

    # ── method extraction ──

    def _extract_ts_method(self, node, source: bytes, result: ParseResult,
                           parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract method_declaration: public static void main(String[] args) { ... }."""
        name = ""
        params = []
        return_type = None
        decorators = []

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "modifiers":
                decorators = self._extract_modifiers(child, source)
            elif child.type == "formal_parameters":
                params = self._extract_ts_params(child, source)
            elif child.type in ("type_identifier", "generic_type",
                                "scoped_type_identifier", "void_type",
                                "integral_type", "floating_point_type",
                                "boolean_type", "array_type"):
                return_type = self._node_text(child, source)
            elif child.type == "type_parameters":
                # Generic method: <T> void foo(T arg)
                pass

        docstring = self._get_ts_javadoc(node, source)
        complexity = self._compute_ts_complexity(node)

        kind = "method" if (parent_symbol and
                            parent_symbol.kind in ("class", "interface")) else "function"

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

    # ── constructor extraction ──

    def _extract_ts_constructor(self, node, source: bytes, result: ParseResult,
                                parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract constructor_declaration: public Foo(int x) { ... }."""
        name = ""
        params = []
        decorators = []

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "modifiers":
                decorators = self._extract_modifiers(child, source)
            elif child.type == "formal_parameters":
                params = self._extract_ts_params(child, source)

        docstring = self._get_ts_javadoc(node, source)
        complexity = self._compute_ts_complexity(node)

        sym = Symbol(
            kind="method",
            name=name,
            params_json=json.dumps(params),
            return_type=None,
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

    # ── modifier and annotation extraction ──

    def _extract_modifiers(self, node, source: bytes) -> list[str]:
        """Extract modifiers and annotations from a modifiers node.

        Includes: public, private, protected, static, final, abstract,
        synchronized, native, transient, volatile, @annotations.
        """
        decorators = []
        for child in node.children:
            if child.type == "marker_annotation":
                # @Override
                dec_text = self._node_text(child, source)
                decorators.append(dec_text)
            elif child.type == "annotation":
                # @SuppressWarnings("unchecked")
                dec_text = self._node_text(child, source)
                decorators.append(dec_text)
            elif child.type in ("public", "private", "protected", "static",
                                "final", "abstract", "synchronized", "native",
                                "transient", "volatile", "default", "strictfp"):
                decorators.append(self._node_text(child, source))
        return decorators

    # ── parameter extraction ──

    def _extract_ts_params(self, node, source: bytes) -> list[dict]:
        """Extract parameters from a formal_parameters node."""
        params = []
        for child in node.children:
            if child.type == "formal_parameter":
                p = self._parse_ts_param(child, source)
                if p:
                    params.append(p)
            elif child.type == "spread_parameter":
                p = self._parse_ts_spread_param(child, source)
                if p:
                    params.append(p)
        return params

    def _parse_ts_param(self, node, source: bytes) -> Optional[dict]:
        """Parse a formal_parameter: Type name or final Type name."""
        name = ""
        type_str = None
        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type in ("type_identifier", "generic_type",
                                "scoped_type_identifier", "integral_type",
                                "floating_point_type", "boolean_type",
                                "array_type", "void_type"):
                type_str = self._node_text(child, source)
            elif child.type == "modifiers":
                # final, annotations on params — skip
                continue
            elif child.type == "dimensions":
                # int[] x → append to type
                if type_str:
                    type_str += self._node_text(child, source)

        if not name:
            return None
        p: dict = {"name": name}
        if type_str:
            p["type"] = type_str
        return p

    def _parse_ts_spread_param(self, node, source: bytes) -> Optional[dict]:
        """Parse a spread_parameter (varargs): Type... name."""
        name = ""
        type_str = None
        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type in ("type_identifier", "generic_type",
                                "scoped_type_identifier", "integral_type",
                                "floating_point_type", "boolean_type",
                                "array_type"):
                type_str = self._node_text(child, source)

        if not name:
            return None
        p: dict = {"name": "..." + name}
        if type_str:
            p["type"] = type_str + "..."
        return p

    # ── call extraction ──

    def _extract_ts_method_call(self, node, source: bytes, result: ParseResult,
                                parent_symbol: Optional[Symbol]):
        """Extract method_invocation: obj.method(args) or method(args)."""
        callee_parts = []
        for child in node.children:
            if child.type == "identifier":
                callee_parts.append(self._node_text(child, source))
            elif child.type in ("field_access", "method_invocation"):
                callee_parts.insert(0, self._node_text(child, source))
            elif child.type == "argument_list":
                break
            elif child.type == ".":
                continue
            elif child.type in ("type_identifier", "scoped_type_identifier",
                                "super", "this"):
                callee_parts.insert(0, self._node_text(child, source))

        callee = ".".join(callee_parts) if callee_parts else ""
        if not callee:
            callee = self._node_text(node, source).split("(")[0].strip()

        call = Call(
            callee_expr=callee,
            line_no=node.start_point[0] + 1,
        )
        call._pending_caller = parent_symbol
        result.calls.append(call)

    def _extract_ts_new_call(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Optional[Symbol]):
        """Extract object_creation_expression: new Foo(args)."""
        type_name = ""
        for child in node.children:
            if child.type in ("type_identifier", "generic_type",
                              "scoped_type_identifier"):
                type_name = self._node_text(child, source)
                break

        if type_name:
            call = Call(
                callee_expr="new " + type_name,
                line_no=node.start_point[0] + 1,
            )
            call._pending_caller = parent_symbol
            result.calls.append(call)

    # ── reference extraction ──

    def _extract_ts_ref(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol]):
        """Extract field_access: obj.field as a reference."""
        full_text = self._node_text(node, source)
        parts = full_text.split(".")
        if len(parts) < 2:
            return

        target = parts[0]
        name = parts[1]

        # Track this.field patterns
        if target == "this":
            ref = Ref(
                ref_kind="read",
                target="this",
                name=name,
                line_no=node.start_point[0] + 1,
            )
            ref._pending_symbol = parent_symbol
            result.refs.append(ref)
        else:
            ref = Ref(
                ref_kind="read",
                target=target,
                name=name,
                line_no=node.start_point[0] + 1,
            )
            ref._pending_symbol = parent_symbol
            result.refs.append(ref)

    # ── javadoc / comment extraction ──

    def _get_ts_javadoc(self, node, source: bytes) -> Optional[str]:
        """Extract Javadoc or block comment preceding a node.

        Javadoc: /** ... */
        Line comments: // ...
        """
        comments = []

        # Check previous siblings for comments
        prev = node.prev_named_sibling
        while prev and prev.type in ("line_comment", "block_comment",
                                      "marker_annotation", "annotation",
                                      "modifiers"):
            if prev.type == "line_comment":
                text = self._node_text(prev, source)
                if text.startswith("//"):
                    text = text[2:].strip()
                comments.insert(0, text)
            elif prev.type == "block_comment":
                text = self._node_text(prev, source)
                if text.startswith("/**") and text.endswith("*/"):
                    # Javadoc — strip markers and clean up
                    text = text[3:-2].strip()
                    lines = text.split("\n")
                    cleaned = []
                    for line in lines:
                        line = line.strip()
                        if line.startswith("*"):
                            line = line[1:].strip()
                        cleaned.append(line)
                    return "\n".join(cleaned)
                elif text.startswith("/*") and text.endswith("*/"):
                    text = text[2:-2].strip()
                    comments.insert(0, text)
            prev = prev.prev_named_sibling

        if comments:
            return "\n".join(comments)
        return None

    # ── cyclomatic complexity ──

    def _compute_ts_complexity(self, node) -> int:
        """Compute cyclomatic complexity for a method body."""
        complexity = 1
        for child in self._walk_all(node):
            if child.type in ("if_statement", "for_statement",
                              "enhanced_for_statement", "while_statement",
                              "do_statement"):
                complexity += 1
            elif child.type == "catch_clause":
                complexity += 1
            elif child.type == "switch_expression_arm":
                complexity += 1
            elif child.type == "ternary_expression":
                complexity += 1
            elif child.type == "binary_expression":
                op = self._get_binary_op(child)
                if op in ("&&", "||"):
                    complexity += 1
            elif child.type == "switch_block_statement_group":
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
