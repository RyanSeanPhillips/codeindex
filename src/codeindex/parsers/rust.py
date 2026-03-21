"""
Rust parser using tree-sitter.

Extracts: functions, methods, structs, enums, traits, impl blocks, calls,
macro invocations, use declarations, attribute refs.
"""

from __future__ import annotations

import json
from typing import Optional

from .base import LanguageParser, ParseResult
from ..store.models import Call, Import, Ref, Symbol

# Try tree-sitter; gracefully degrade if not installed
_TS_AVAILABLE = False
try:
    import tree_sitter_rust as tsrust
    from tree_sitter import Language, Parser as TSParser

    RUST_LANGUAGE = Language(tsrust.language())
    _TS_AVAILABLE = True
except Exception:
    pass


class RustParser(LanguageParser):
    """Rust source parser. Requires tree-sitter-rust."""

    @property
    def language(self) -> str:
        return "rust"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".rs",)

    def parse(self, source: str, rel_path: str) -> ParseResult:
        if not _TS_AVAILABLE:
            result = ParseResult()
            result.parse_error = "tree-sitter-rust not available"
            return result
        return self._parse_tree_sitter(source, rel_path)

    # ── tree-sitter implementation ──

    def _parse_tree_sitter(self, source: str, rel_path: str) -> ParseResult:
        parser = TSParser(RUST_LANGUAGE)
        tree = parser.parse(source.encode("utf-8"))
        result = ParseResult()

        if tree.root_node.has_error:
            result.parse_error = "tree-sitter parse error"

        source_bytes = source.encode("utf-8")
        self._walk_ts(tree.root_node, source_bytes, result, parent_symbol=None)
        return result

    def _walk_ts(self, node, source: bytes, result: ParseResult,
                 parent_symbol: Optional[Symbol], depth: int = 0):
        if node.type == "use_declaration":
            self._extract_ts_use(node, source, result)
        elif node.type == "function_item":
            sym = self._extract_ts_function(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "impl_item":
            impl_sym = self._extract_ts_impl(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=impl_sym, depth=depth + 1)
            return
        elif node.type == "struct_item":
            sym = self._extract_ts_struct(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "enum_item":
            sym = self._extract_ts_enum(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "trait_item":
            sym = self._extract_ts_trait(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "call_expression":
            self._extract_ts_call(node, source, result, parent_symbol)
        elif node.type == "macro_invocation":
            self._extract_ts_macro_call(node, source, result, parent_symbol)
        elif node.type == "field_expression":
            self._extract_ts_ref(node, source, result, parent_symbol)

        for child in node.children:
            self._walk_ts(child, source, result, parent_symbol=parent_symbol, depth=depth + 1)

    def _node_text(self, node, source: bytes) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    # ── use declarations (imports) ──

    def _extract_ts_use(self, node, source: bytes, result: ParseResult):
        """Extract use declarations: use std::io::Read; use crate::module::{A, B};"""
        # Collect the full path from the use_declaration
        for child in node.children:
            if child.type == "use_as_clause":
                self._extract_use_as_clause(child, source, result, node)
                return
            elif child.type == "scoped_use_list":
                self._extract_scoped_use_list(child, source, result, node)
                return
            elif child.type == "scoped_identifier":
                module = self._node_text(child, source)
                parts = module.rsplit("::", 1)
                if len(parts) == 2:
                    result.imports.append(Import(
                        module=parts[0],
                        name=parts[1],
                        is_from=True,
                        line_no=node.start_point[0] + 1,
                    ))
                else:
                    result.imports.append(Import(
                        module=module,
                        is_from=False,
                        line_no=node.start_point[0] + 1,
                    ))
                return
            elif child.type == "identifier":
                result.imports.append(Import(
                    module=self._node_text(child, source),
                    is_from=False,
                    line_no=node.start_point[0] + 1,
                ))
                return

    def _extract_use_as_clause(self, node, source: bytes, result: ParseResult,
                               use_node):
        """Extract: use std::io::Read as IoRead;"""
        path_text = ""
        alias = None
        for child in node.children:
            if child.type in ("scoped_identifier", "identifier"):
                path_text = self._node_text(child, source)
            elif child.type == "identifier" and alias is None and path_text:
                alias = self._node_text(child, source)

        # Re-parse: first non-keyword child is path, last identifier after "as" is alias
        children = [c for c in node.children if c.type not in ("use", ";")]
        if len(children) >= 3:
            path_text = self._node_text(children[0], source)
            alias = self._node_text(children[2], source)

        parts = path_text.rsplit("::", 1)
        if len(parts) == 2:
            result.imports.append(Import(
                module=parts[0], name=parts[1], alias=alias,
                is_from=True, line_no=use_node.start_point[0] + 1,
            ))
        else:
            result.imports.append(Import(
                module=path_text, alias=alias,
                is_from=False, line_no=use_node.start_point[0] + 1,
            ))

    def _extract_scoped_use_list(self, node, source: bytes, result: ParseResult,
                                 use_node):
        """Extract: use std::{io, fs}; or use crate::module::{A, B};"""
        # The path prefix is the scoped_identifier or identifier before the use_list
        prefix = ""
        for child in node.children:
            if child.type == "scoped_identifier":
                prefix = self._node_text(child, source)
            elif child.type == "identifier" and not prefix:
                prefix = self._node_text(child, source)
            elif child.type == "self":
                # use module::{self, Foo}
                result.imports.append(Import(
                    module=prefix,
                    is_from=False,
                    line_no=use_node.start_point[0] + 1,
                ))
            elif child.type == "use_list":
                for item in child.children:
                    if item.type == "identifier":
                        name = self._node_text(item, source)
                        result.imports.append(Import(
                            module=prefix, name=name,
                            is_from=True, line_no=use_node.start_point[0] + 1,
                        ))
                    elif item.type == "scoped_identifier":
                        full = self._node_text(item, source)
                        result.imports.append(Import(
                            module=prefix, name=full,
                            is_from=True, line_no=use_node.start_point[0] + 1,
                        ))
                    elif item.type == "use_as_clause":
                        self._extract_use_as_clause(item, source, result, use_node)
                    elif item.type == "self":
                        result.imports.append(Import(
                            module=prefix,
                            is_from=False,
                            line_no=use_node.start_point[0] + 1,
                        ))

    # ── function extraction ──

    def _extract_ts_function(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract a function_item: fn name(params) -> ReturnType { body }."""
        name = ""
        params = []
        return_type = None
        decorators = []
        is_async = False

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "parameters":
                params = self._extract_ts_params(child, source)
            elif child.type == "type_identifier" or (child.type in (
                    "generic_type", "scoped_type_identifier", "reference_type",
                    "tuple_type", "unit_type", "primitive_type",
                    "function_type", "array_type", "pointer_type") and
                    child.prev_sibling and self._node_text(child.prev_sibling, source) == "->"):
                return_type = self._node_text(child, source)
            elif child.type == "visibility_modifier":
                decorators.append(self._node_text(child, source))
            elif child.type == "function_modifiers":
                for mod in child.children:
                    if mod.type == "async":
                        is_async = True

        # Capture return type more reliably: find child after "->"
        found_arrow = False
        for child in node.children:
            if found_arrow and child.type not in ("block", "{", "}",
                                                   "where_clause", "line_comment",
                                                   "block_comment"):
                return_type = self._node_text(child, source)
                break
            if child.type == "->" or self._node_text(child, source) == "->":
                found_arrow = True

        # Collect attribute decorators from preceding siblings
        prev = node.prev_named_sibling
        while prev and prev.type == "attribute_item":
            dec_text = self._node_text(prev, source).strip()
            decorators.insert(0, dec_text)
            prev = prev.prev_named_sibling

        docstring = self._get_ts_doc_comment(node, source)
        complexity = self._compute_ts_complexity(node)

        # Determine kind: method if inside impl block
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
            is_async=is_async,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    # ── impl block extraction ──

    def _extract_ts_impl(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]) -> Optional[Symbol]:
        """Extract impl block. Return the struct/type symbol as parent for methods.

        impl MyStruct { ... } → methods with parent=MyStruct
        impl Trait for Struct { ... } → methods with parent=Struct
        """
        type_name = None
        trait_name = None

        children = list(node.children)
        # Pattern: impl [Trait for] Type { body }
        # Find type identifiers
        type_ids = []
        for child in children:
            if child.type in ("type_identifier", "generic_type",
                              "scoped_type_identifier"):
                type_ids.append(self._node_text(child, source))

        # Check for "for" keyword to distinguish trait impl
        has_for = any(self._node_text(c, source) == "for" for c in children)

        if has_for and len(type_ids) >= 2:
            trait_name = type_ids[0]
            type_name = type_ids[1]
        elif type_ids:
            type_name = type_ids[0]

        if not type_name:
            return parent_symbol

        # Find existing struct/trait symbol to use as parent
        for s in result.symbols:
            if s.kind in ("class", "interface") and s.name == type_name:
                return s

        # Create a synthetic class symbol for the impl target if not found
        sym = Symbol(
            kind="class",
            name=type_name,
            bases_json=json.dumps([trait_name] if trait_name else []),
            decorators_json=json.dumps([]),
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    # ── struct extraction ──

    def _extract_ts_struct(self, node, source: bytes, result: ParseResult,
                           parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract struct_item: struct Name { fields }."""
        name = ""
        decorators = []

        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)
            elif child.type == "visibility_modifier":
                decorators.append(self._node_text(child, source))

        # Collect attribute decorators from preceding siblings
        prev = node.prev_named_sibling
        while prev and prev.type == "attribute_item":
            dec_text = self._node_text(prev, source).strip()
            decorators.insert(0, dec_text)
            prev = prev.prev_named_sibling

        docstring = self._get_ts_doc_comment(node, source)

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
        return sym

    # ── enum extraction ──

    def _extract_ts_enum(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract enum_item: enum Name { Variant1, Variant2 }."""
        name = ""
        decorators = []

        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)
            elif child.type == "visibility_modifier":
                decorators.append(self._node_text(child, source))

        prev = node.prev_named_sibling
        while prev and prev.type == "attribute_item":
            dec_text = self._node_text(prev, source).strip()
            decorators.insert(0, dec_text)
            prev = prev.prev_named_sibling

        docstring = self._get_ts_doc_comment(node, source)

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
        return sym

    # ── trait extraction ──

    def _extract_ts_trait(self, node, source: bytes, result: ParseResult,
                          parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract trait_item: trait Name { fn method(&self); }."""
        name = ""
        decorators = []
        bases = []

        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)
            elif child.type == "visibility_modifier":
                decorators.append(self._node_text(child, source))
            elif child.type == "trait_bounds":
                # Supertraits: trait Foo: Bar + Baz
                for bound in child.children:
                    if bound.type in ("type_identifier", "generic_type",
                                      "scoped_type_identifier"):
                        bases.append(self._node_text(bound, source))

        prev = node.prev_named_sibling
        while prev and prev.type == "attribute_item":
            dec_text = self._node_text(prev, source).strip()
            decorators.insert(0, dec_text)
            prev = prev.prev_named_sibling

        docstring = self._get_ts_doc_comment(node, source)

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

    # ── parameter extraction ──

    def _extract_ts_params(self, node, source: bytes) -> list[dict]:
        """Extract parameters from a parameters node."""
        params = []
        for child in node.children:
            if child.type == "parameter":
                p = self._parse_ts_param(child, source)
                if p:
                    params.append(p)
            elif child.type == "self_parameter":
                # Skip &self, &mut self, self
                continue
        return params

    def _parse_ts_param(self, node, source: bytes) -> Optional[dict]:
        """Parse a single parameter: name: Type."""
        name = ""
        type_str = None
        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "mutable_specifier":
                continue
            elif child.type == ":":
                continue
            elif child.type not in (",", "(", ")") and not name:
                # Pattern parameter
                name = self._node_text(child, source)
            elif child.type not in (",", "(", ")", ":"):
                type_str = self._node_text(child, source)

        if not name:
            return None
        p: dict = {"name": name}
        if type_str:
            p["type"] = type_str
        return p

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

    def _extract_ts_macro_call(self, node, source: bytes, result: ParseResult,
                               parent_symbol: Optional[Symbol]):
        """Extract a macro invocation: println!(...), vec![...], etc."""
        macro_name = ""
        for child in node.children:
            if child.type == "identifier":
                macro_name = self._node_text(child, source)
            elif child.type == "scoped_identifier":
                macro_name = self._node_text(child, source)

        if macro_name:
            call = Call(
                callee_expr=macro_name + "!",
                line_no=node.start_point[0] + 1,
            )
            call._pending_caller = parent_symbol
            result.calls.append(call)

    # ── reference extraction ──

    def _extract_ts_ref(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol]):
        """Extract field_expression: obj.field as a ref."""
        full_text = self._node_text(node, source)
        parts = full_text.split(".")
        if len(parts) < 2:
            return

        target = parts[0]
        name = parts[1]

        # Track self.field patterns
        ref = Ref(
            ref_kind="read",
            target=target,
            name=name,
            line_no=node.start_point[0] + 1,
        )
        ref._pending_symbol = parent_symbol
        result.refs.append(ref)

    # ── doc comment extraction ──

    def _get_ts_doc_comment(self, node, source: bytes) -> Optional[str]:
        """Extract /// doc comments or //! inner doc comments preceding a node."""
        comments = []
        prev = node.prev_named_sibling
        while prev and prev.type in ("line_comment", "block_comment",
                                      "attribute_item"):
            if prev.type in ("line_comment", "block_comment"):
                text = self._node_text(prev, source)
                if text.startswith("///"):
                    text = text[3:].strip()
                    comments.insert(0, text)
                elif text.startswith("//!"):
                    text = text[3:].strip()
                    comments.insert(0, text)
                elif text.startswith("//"):
                    text = text[2:].strip()
                    comments.insert(0, text)
                elif text.startswith("/**") and text.endswith("*/"):
                    text = text[3:-2].strip()
                    comments.insert(0, text)
            prev = prev.prev_named_sibling

        if comments:
            return "\n".join(comments)
        return None

    # ── cyclomatic complexity ──

    def _compute_ts_complexity(self, node) -> int:
        """Compute cyclomatic complexity for a function body."""
        complexity = 1
        for child in self._walk_all(node):
            if child.type in ("if_expression", "for_expression",
                              "while_expression", "loop_expression",
                              "match_expression"):
                complexity += 1
            elif child.type == "match_arm":
                complexity += 1
            elif child.type == "binary_expression":
                op = self._get_binary_op(child)
                if op in ("&&", "||"):
                    complexity += 1
            elif child.type == "let_chain":
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
