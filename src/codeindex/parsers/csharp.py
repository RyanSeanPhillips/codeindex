"""
C# parser using tree-sitter.

Extracts: classes, structs, interfaces, enums, methods, constructors,
properties, using imports, calls, object creation, refs.
"""

from __future__ import annotations

import json
from typing import Optional

from .base import LanguageParser, ParseResult
from ..store.models import Call, Import, Ref, Symbol

# Try tree-sitter; degrade gracefully if unavailable
_TS_AVAILABLE = False
try:
    import tree_sitter_c_sharp as tscsharp
    from tree_sitter import Language, Parser as TSParser

    CS_LANGUAGE = Language(tscsharp.language())
    _TS_AVAILABLE = True
except Exception:
    pass


class CSharpParser(LanguageParser):
    """C# source parser. Uses tree-sitter for .cs files."""

    @property
    def language(self) -> str:
        return "csharp"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".cs",)

    def parse(self, source: str, rel_path: str) -> ParseResult:
        if not _TS_AVAILABLE:
            result = ParseResult()
            result.parse_error = "tree-sitter-c-sharp not available"
            return result
        return self._parse_tree_sitter(source, rel_path)

    # ── tree-sitter implementation ──

    def _parse_tree_sitter(self, source: str, rel_path: str) -> ParseResult:
        parser = TSParser(CS_LANGUAGE)
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
        if node.type == "using_directive":
            self._extract_ts_using(node, source, result)
        elif node.type == "namespace_declaration":
            self._walk_namespace(node, source, result, parent_symbol,
                                 namespace_parts, depth)
            return
        elif node.type == "file_scoped_namespace_declaration":
            self._walk_file_scoped_namespace(node, source, result, parent_symbol,
                                             namespace_parts, depth)
            return
        elif node.type == "class_declaration":
            sym = self._extract_ts_class(node, source, result, parent_symbol,
                                         namespace_parts)
            if sym:
                for child in node.children:
                    self._walk_ts(child, source, result, parent_symbol=sym,
                                  namespace_parts=namespace_parts, depth=depth + 1)
                return
        elif node.type == "struct_declaration":
            sym = self._extract_ts_struct(node, source, result, parent_symbol,
                                          namespace_parts)
            if sym:
                for child in node.children:
                    self._walk_ts(child, source, result, parent_symbol=sym,
                                  namespace_parts=namespace_parts, depth=depth + 1)
                return
        elif node.type == "interface_declaration":
            sym = self._extract_ts_interface(node, source, result, parent_symbol,
                                             namespace_parts)
            if sym:
                for child in node.children:
                    self._walk_ts(child, source, result, parent_symbol=sym,
                                  namespace_parts=namespace_parts, depth=depth + 1)
                return
        elif node.type == "enum_declaration":
            self._extract_ts_enum(node, source, result, parent_symbol,
                                  namespace_parts)
        elif node.type == "method_declaration":
            sym = self._extract_ts_method(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym,
                              namespace_parts=namespace_parts, depth=depth + 1)
            return
        elif node.type == "constructor_declaration":
            sym = self._extract_ts_constructor(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym,
                              namespace_parts=namespace_parts, depth=depth + 1)
            return
        elif node.type == "property_declaration":
            self._extract_ts_property(node, source, result, parent_symbol)
        elif node.type == "invocation_expression":
            self._extract_ts_call(node, source, result, parent_symbol)
        elif node.type == "object_creation_expression":
            self._extract_ts_new(node, source, result, parent_symbol)
        elif node.type == "member_access_expression":
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
        """Walk into a namespace_declaration, tracking the namespace path."""
        ns_name = ""
        for child in node.children:
            if child.type in ("identifier", "qualified_name"):
                ns_name = self._node_text(child, source)

        new_parts = namespace_parts + [ns_name] if ns_name else namespace_parts
        for child in node.children:
            if child.type == "declaration_list":
                for sub in child.children:
                    self._walk_ts(sub, source, result, parent_symbol=parent_symbol,
                                  namespace_parts=new_parts, depth=depth + 1)

    def _walk_file_scoped_namespace(self, node, source: bytes, result: ParseResult,
                                    parent_symbol: Optional[Symbol],
                                    namespace_parts: list[str], depth: int):
        """Walk a file-scoped namespace (C# 10+: namespace Foo.Bar;)."""
        ns_name = ""
        for child in node.children:
            if child.type in ("identifier", "qualified_name"):
                ns_name = self._node_text(child, source)

        new_parts = namespace_parts + [ns_name] if ns_name else namespace_parts
        # File-scoped namespace: all remaining declarations at this level
        for child in node.children:
            if child.type not in ("namespace", "identifier", "qualified_name", ";"):
                self._walk_ts(child, source, result, parent_symbol=parent_symbol,
                              namespace_parts=new_parts, depth=depth + 1)

    # ── Using extraction ──

    def _extract_ts_using(self, node, source: bytes, result: ParseResult):
        """Extract using directives as imports."""
        module = ""
        alias = None
        is_static = False

        for child in node.children:
            if child.type in ("identifier", "qualified_name"):
                module = self._node_text(child, source)
            elif child.type == "name_equals":
                # using Alias = Namespace.Type;
                for subchild in child.children:
                    if subchild.type == "identifier":
                        alias = self._node_text(subchild, source)
            elif child.type == "static":
                is_static = True

        decorators = []
        if is_static:
            decorators.append("static")

        result.imports.append(Import(
            module=module,
            alias=alias,
            is_from=False,
            line_no=node.start_point[0] + 1,
        ))

    # ── Class extraction ──

    def _extract_ts_class(self, node, source: bytes, result: ParseResult,
                          parent_symbol: Optional[Symbol],
                          namespace_parts: list[str]) -> Optional[Symbol]:
        """Extract a class_declaration."""
        name = ""
        bases = []
        decorators = []

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "base_list":
                bases = self._extract_bases(child, source)
            elif child.type == "modifier":
                decorators.append(self._node_text(child, source))
            elif child.type == "attribute_list":
                attrs = self._extract_attributes(child, source)
                decorators.extend(attrs)

        if not name:
            return None

        if namespace_parts:
            decorators.insert(0, "namespace:" + ".".join(namespace_parts))

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

    # ── Struct extraction ──

    def _extract_ts_struct(self, node, source: bytes, result: ParseResult,
                           parent_symbol: Optional[Symbol],
                           namespace_parts: list[str]) -> Optional[Symbol]:
        """Extract a struct_declaration as a class symbol."""
        name = ""
        bases = []
        decorators = ["struct"]

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "base_list":
                bases = self._extract_bases(child, source)
            elif child.type == "modifier":
                decorators.append(self._node_text(child, source))
            elif child.type == "attribute_list":
                attrs = self._extract_attributes(child, source)
                decorators.extend(attrs)

        if not name:
            return None

        if namespace_parts:
            decorators.insert(0, "namespace:" + ".".join(namespace_parts))

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

    # ── Interface extraction ──

    def _extract_ts_interface(self, node, source: bytes, result: ParseResult,
                              parent_symbol: Optional[Symbol],
                              namespace_parts: list[str]) -> Optional[Symbol]:
        """Extract an interface_declaration."""
        name = ""
        bases = []
        decorators = []

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "base_list":
                bases = self._extract_bases(child, source)
            elif child.type == "modifier":
                decorators.append(self._node_text(child, source))
            elif child.type == "attribute_list":
                attrs = self._extract_attributes(child, source)
                decorators.extend(attrs)

        if not name:
            return None

        if namespace_parts:
            decorators.insert(0, "namespace:" + ".".join(namespace_parts))

        docstring = self._get_preceding_comment(node, source)

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

    # ── Enum extraction ──

    def _extract_ts_enum(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol],
                         namespace_parts: list[str]):
        """Extract an enum_declaration as a class symbol."""
        name = ""
        decorators = ["enum"]

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "modifier":
                decorators.append(self._node_text(child, source))
            elif child.type == "attribute_list":
                attrs = self._extract_attributes(child, source)
                decorators.extend(attrs)

        if not name:
            return

        if namespace_parts:
            decorators.insert(0, "namespace:" + ".".join(namespace_parts))

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

    # ── Method extraction ──

    def _extract_ts_method(self, node, source: bytes, result: ParseResult,
                           parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract a method_declaration."""
        name = ""
        params = []
        return_type = None
        decorators = []
        is_async = False

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "parameter_list":
                params = self._extract_params(child, source)
            elif child.type in ("predefined_type", "generic_name",
                                "qualified_name", "identifier",
                                "nullable_type", "array_type", "tuple_type"):
                # First type-like child before the name is the return type
                if not return_type and not name:
                    return_type = self._node_text(child, source)
            elif child.type == "modifier":
                mod = self._node_text(child, source)
                if mod == "async":
                    is_async = True
                else:
                    decorators.append(mod)
            elif child.type == "attribute_list":
                attrs = self._extract_attributes(child, source)
                decorators.extend(attrs)

        # Re-scan for return type if we didn't get it
        if not return_type:
            return_type = self._find_return_type(node, source)

        docstring = self._get_preceding_comment(node, source)
        complexity = self._compute_complexity(node)

        kind = "method" if parent_symbol and parent_symbol.kind in ("class", "interface") else "function"

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

    def _find_return_type(self, node, source: bytes) -> Optional[str]:
        """Find the return type for a method by scanning type-like children."""
        type_node_types = (
            "predefined_type", "generic_name", "qualified_name",
            "nullable_type", "array_type", "tuple_type", "void_keyword",
        )
        for child in node.children:
            if child.type in type_node_types:
                return self._node_text(child, source)
            if child.type == "identifier":
                # Could be a type name if it appears before the method name
                # Heuristic: skip if it matches the method's own name
                next_sib = child.next_named_sibling
                if next_sib and next_sib.type in ("identifier", "parameter_list"):
                    return self._node_text(child, source)
        return None

    # ── Constructor extraction ──

    def _extract_ts_constructor(self, node, source: bytes, result: ParseResult,
                                parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract a constructor_declaration."""
        name = ""
        params = []
        decorators = ["constructor"]
        is_async = False

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "parameter_list":
                params = self._extract_params(child, source)
            elif child.type == "modifier":
                mod = self._node_text(child, source)
                if mod == "async":
                    is_async = True
                else:
                    decorators.append(mod)
            elif child.type == "attribute_list":
                attrs = self._extract_attributes(child, source)
                decorators.extend(attrs)

        docstring = self._get_preceding_comment(node, source)
        complexity = self._compute_complexity(node)

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
            is_async=is_async,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    # ── Property extraction ──

    def _extract_ts_property(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Optional[Symbol]):
        """Extract a property_declaration as a method symbol."""
        name = ""
        return_type = None
        decorators = ["property"]

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type in ("predefined_type", "generic_name",
                                "qualified_name", "nullable_type",
                                "array_type", "tuple_type"):
                if not return_type:
                    return_type = self._node_text(child, source)
            elif child.type == "modifier":
                decorators.append(self._node_text(child, source))
            elif child.type == "attribute_list":
                attrs = self._extract_attributes(child, source)
                decorators.extend(attrs)

        if not name:
            return

        docstring = self._get_preceding_comment(node, source)

        sym = Symbol(
            kind="method",
            name=name,
            params_json=json.dumps([]),
            return_type=return_type,
            decorators_json=json.dumps(decorators),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)

    # ── Parameter extraction ──

    def _extract_params(self, node, source: bytes) -> list[dict]:
        """Extract method parameters from a parameter_list node."""
        params = []
        for child in node.children:
            if child.type == "parameter":
                param = self._parse_param(child, source)
                if param:
                    params.append(param)
        return params

    def _parse_param(self, node, source: bytes) -> Optional[dict]:
        """Parse a single parameter node."""
        name = ""
        type_str = None
        default = None
        modifiers = []

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type in ("predefined_type", "generic_name",
                                "qualified_name", "nullable_type",
                                "array_type", "tuple_type", "type_identifier"):
                type_str = self._node_text(child, source)
            elif child.type == "equals_value_clause":
                # Default value
                for subchild in child.children:
                    if subchild.type != "=":
                        default = self._node_text(subchild, source)
                        break
            elif child.type == "parameter_modifier":
                modifiers.append(self._node_text(child, source))

        if not name:
            return None

        result: dict = {"name": name}
        if type_str:
            result["type"] = type_str
        if default:
            result["default"] = default
        if modifiers:
            result["modifiers"] = modifiers
        return result

    # ── Base list extraction ──

    def _extract_bases(self, base_list_node, source: bytes) -> list[str]:
        """Extract base types from a base_list node."""
        bases = []
        for child in base_list_node.children:
            if child.type in ("identifier", "qualified_name", "generic_name"):
                bases.append(self._node_text(child, source))
        return bases

    # ── Attribute extraction ──

    def _extract_attributes(self, attr_list_node, source: bytes) -> list[str]:
        """Extract [Attribute] annotations from an attribute_list node."""
        attrs = []
        for child in attr_list_node.children:
            if child.type == "attribute":
                attr_text = self._node_text(child, source)
                attrs.append(f"[{attr_text}]")
        return attrs

    # ── Call extraction ──

    def _extract_ts_call(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]):
        """Extract an invocation_expression as a call."""
        func_node = None
        for child in node.children:
            if child.type in ("identifier", "member_access_expression",
                              "qualified_name", "generic_name",
                              "member_binding_expression"):
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
        """Extract an object_creation_expression as a constructor call."""
        type_name = ""
        for child in node.children:
            if child.type in ("identifier", "qualified_name", "generic_name"):
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
        """Extract member_access_expression (e.g. obj.Property) as a ref."""
        full_text = self._node_text(node, source)

        if "." not in full_text:
            return

        parts = full_text.split(".", 1)
        if len(parts) != 2:
            return

        target = parts[0].strip()
        name = parts[1].strip()

        # Take only the first member for nested access
        if "." in name:
            name = name.split(".", 1)[0].strip()

        # Track this.X patterns (C# equivalent of self.X)
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

    # ── Comment / docstring extraction ──

    def _get_preceding_comment(self, node, source: bytes) -> Optional[str]:
        """Get the comment block immediately preceding a node.

        Handles /// XML doc comments, /* */ block comments, and // line comments.
        """
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
            if text.startswith("///"):
                # XML doc comment — strip /// and basic XML tags
                line = text[3:].strip()
                line = self._strip_xml_tags(line)
                lines.append(line)
            elif text.startswith("/*") and text.endswith("*/"):
                text = text[2:-2].strip()
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("*"):
                        line = line[1:].strip()
                    lines.append(line)
            elif text.startswith("//"):
                lines.append(text[2:].strip())

        return "\n".join(lines).strip() if lines else None

    def _strip_xml_tags(self, text: str) -> str:
        """Remove XML tags like <summary>, <param>, etc. from doc comments."""
        import re
        return re.sub(r"<[^>]+>", "", text).strip()

    # ── Complexity computation ──

    def _compute_complexity(self, node) -> int:
        """Compute cyclomatic complexity for a method."""
        complexity = 1
        for child in self._walk_all(node):
            if child.type in ("if_statement", "for_statement",
                              "for_each_statement", "while_statement",
                              "do_statement", "case_switch_label"):
                complexity += 1
            elif child.type == "conditional_expression":
                complexity += 1
            elif child.type == "catch_clause":
                complexity += 1
            elif child.type in ("&&", "||",
                                "and_pattern", "or_pattern"):
                complexity += 1
            elif child.type == "switch_expression_arm":
                complexity += 1
        return complexity

    def _walk_all(self, node):
        """Yield all descendant nodes."""
        for child in node.children:
            yield child
            yield from self._walk_all(child)
