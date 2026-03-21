"""
TypeScript/TSX parser using tree-sitter.

Extracts: classes, interfaces, functions/methods, arrow functions,
enums, type aliases, calls, imports, attribute refs.
"""

from __future__ import annotations

import json
from typing import Optional

from .base import LanguageParser, ParseResult
from ..store.models import Call, Import, Ref, Symbol

# Try tree-sitter; gracefully degrade if unavailable
_TS_AVAILABLE = False
_TSX_AVAILABLE = False
try:
    import tree_sitter_typescript as tstypescript
    from tree_sitter import Language, Parser as TSParser

    TS_LANGUAGE = Language(tstypescript.language_typescript())
    _TS_AVAILABLE = True
    try:
        TSX_LANGUAGE = Language(tstypescript.language_tsx())
        _TSX_AVAILABLE = True
    except Exception:
        TSX_LANGUAGE = TS_LANGUAGE  # fallback to TS parser for TSX
        _TSX_AVAILABLE = True
except Exception:
    pass


class TypeScriptParser(LanguageParser):
    """TypeScript/TSX source parser using tree-sitter."""

    @property
    def language(self) -> str:
        return "typescript"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".ts", ".tsx")

    def parse(self, source: str, rel_path: str) -> ParseResult:
        if not _TS_AVAILABLE:
            return ParseResult(parse_error="tree-sitter-typescript not available")
        return self._parse_tree_sitter(source, rel_path)

    # ── tree-sitter implementation ──

    def _parse_tree_sitter(self, source: str, rel_path: str) -> ParseResult:
        # Use TSX language for .tsx files, TS language otherwise
        lang = TSX_LANGUAGE if rel_path.endswith(".tsx") else TS_LANGUAGE
        parser = TSParser(lang)
        tree = parser.parse(source.encode("utf-8"))
        result = ParseResult()

        if tree.root_node.has_error:
            result.parse_error = "tree-sitter parse error"

        source_bytes = source.encode("utf-8")
        self._walk_ts(tree.root_node, source_bytes, result, parent_symbol=None)
        return result

    def _walk_ts(self, node, source: bytes, result: ParseResult,
                 parent_symbol: Optional[Symbol], depth: int = 0):
        if node.type == "import_statement":
            self._extract_ts_import(node, source, result)
        elif node.type == "export_statement":
            # An export wraps a declaration; walk children with "export" as decorator
            self._extract_ts_export(node, source, result, parent_symbol, depth)
            return
        elif node.type == "class_declaration":
            sym = self._extract_ts_class(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "abstract_class_declaration":
            sym = self._extract_ts_class(node, source, result, parent_symbol,
                                         extra_decorators=["abstract"])
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "interface_declaration":
            sym = self._extract_ts_interface(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "type_alias_declaration":
            self._extract_ts_type_alias(node, source, result, parent_symbol)
        elif node.type == "enum_declaration":
            sym = self._extract_ts_enum(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "function_declaration":
            sym = self._extract_ts_function(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "method_definition":
            sym = self._extract_ts_method(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "lexical_declaration":
            # Check for arrow function assignments: const foo = (...) => { ... }
            handled = self._try_extract_arrow_function(node, source, result, parent_symbol, depth)
            if handled:
                return
        elif node.type == "call_expression":
            self._extract_ts_call(node, source, result, parent_symbol)
        elif node.type == "new_expression":
            self._extract_ts_new(node, source, result, parent_symbol)
        elif node.type == "member_expression":
            self._extract_ts_ref(node, source, result, parent_symbol)

        for child in node.children:
            self._walk_ts(child, source, result, parent_symbol=parent_symbol, depth=depth + 1)

    # ── helpers ──

    def _node_text(self, node, source: bytes) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _find_child(self, node, type_name: str):
        """Find the first child with the given type."""
        for child in node.children:
            if child.type == type_name:
                return child
        return None

    def _find_children(self, node, type_name: str) -> list:
        """Find all children with the given type."""
        return [c for c in node.children if c.type == type_name]

    # ── imports ──

    def _extract_ts_import(self, node, source: bytes, result: ParseResult):
        """Parse import statements.

        Handles:
          import { foo, bar } from 'module'
          import foo from 'module'
          import * as foo from 'module'
          import 'module'  (side-effect import)
        """
        module = ""
        # Find the module string (source)
        string_node = self._find_child(node, "string")
        if string_node:
            module = self._node_text(string_node, source).strip("'\"")

        line_no = node.start_point[0] + 1

        import_clause = self._find_child(node, "import_clause")
        if not import_clause:
            # Side-effect import: import 'module'
            result.imports.append(Import(
                module=module, is_from=False, line_no=line_no,
            ))
            return

        for child in import_clause.children:
            if child.type == "identifier":
                # Default import: import foo from 'module'
                name = self._node_text(child, source)
                result.imports.append(Import(
                    module=module, name=name, is_from=True, line_no=line_no,
                ))
            elif child.type == "named_imports":
                # Named imports: import { foo, bar as baz } from 'module'
                for spec in child.children:
                    if spec.type == "import_specifier":
                        name_node = spec.children[0] if spec.children else None
                        alias_node = None
                        # import_specifier: name "as" alias
                        if spec.child_count >= 3:
                            alias_node = spec.children[-1]
                        name = self._node_text(name_node, source) if name_node else ""
                        alias = self._node_text(alias_node, source) if alias_node else None
                        result.imports.append(Import(
                            module=module, name=name, alias=alias,
                            is_from=True, line_no=line_no,
                        ))
            elif child.type == "namespace_import":
                # Namespace import: import * as foo from 'module'
                alias_node = self._find_child(child, "identifier")
                alias = self._node_text(alias_node, source) if alias_node else None
                result.imports.append(Import(
                    module=module, alias=alias, is_from=False, line_no=line_no,
                ))

    # ── exports (decorator wrapper) ──

    def _extract_ts_export(self, node, source: bytes, result: ParseResult,
                           parent_symbol: Optional[Symbol], depth: int):
        """Handle export statement — walk children and mark exported declarations."""
        for child in node.children:
            if child.type == "class_declaration":
                sym = self._extract_ts_class(child, source, result, parent_symbol,
                                             extra_decorators=["export"])
                for sub in child.children:
                    self._walk_ts(sub, source, result, parent_symbol=sym, depth=depth + 1)
            elif child.type == "abstract_class_declaration":
                sym = self._extract_ts_class(child, source, result, parent_symbol,
                                             extra_decorators=["export", "abstract"])
                for sub in child.children:
                    self._walk_ts(sub, source, result, parent_symbol=sym, depth=depth + 1)
            elif child.type == "function_declaration":
                sym = self._extract_ts_function(child, source, result, parent_symbol,
                                                extra_decorators=["export"])
                for sub in child.children:
                    self._walk_ts(sub, source, result, parent_symbol=sym, depth=depth + 1)
            elif child.type == "interface_declaration":
                sym = self._extract_ts_interface(child, source, result, parent_symbol,
                                                 extra_decorators=["export"])
                for sub in child.children:
                    self._walk_ts(sub, source, result, parent_symbol=sym, depth=depth + 1)
            elif child.type == "type_alias_declaration":
                self._extract_ts_type_alias(child, source, result, parent_symbol,
                                            extra_decorators=["export"])
            elif child.type == "enum_declaration":
                sym = self._extract_ts_enum(child, source, result, parent_symbol,
                                            extra_decorators=["export"])
                for sub in child.children:
                    self._walk_ts(sub, source, result, parent_symbol=sym, depth=depth + 1)
            elif child.type == "lexical_declaration":
                handled = self._try_extract_arrow_function(
                    child, source, result, parent_symbol, depth,
                    extra_decorators=["export"])
                if not handled:
                    self._walk_ts(child, source, result, parent_symbol=parent_symbol,
                                  depth=depth + 1)
            else:
                self._walk_ts(child, source, result, parent_symbol=parent_symbol,
                              depth=depth + 1)

    # ── classes ──

    def _extract_ts_class(self, node, source: bytes, result: ParseResult,
                          parent_symbol: Optional[Symbol],
                          extra_decorators: Optional[list[str]] = None) -> Symbol:
        name = ""
        bases = []
        decorators = list(extra_decorators) if extra_decorators else []

        # Collect decorators from preceding siblings
        prev = node.prev_named_sibling
        while prev and prev.type == "decorator":
            dec_text = self._node_text(prev, source).lstrip("@").strip()
            decorators.insert(0, dec_text)
            prev = prev.prev_named_sibling

        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)
            elif child.type in ("class_heritage", "extends_clause"):
                # Extract base classes and implemented interfaces
                for heritage_child in child.children:
                    if heritage_child.type == "extends_clause":
                        for ext in heritage_child.children:
                            if ext.type in ("identifier", "type_identifier",
                                            "generic_type", "nested_type_identifier"):
                                bases.append(self._node_text(ext, source))
                    elif heritage_child.type == "implements_clause":
                        for impl in heritage_child.children:
                            if impl.type in ("identifier", "type_identifier",
                                             "generic_type", "nested_type_identifier"):
                                bases.append(self._node_text(impl, source))
                    elif heritage_child.type in ("identifier", "type_identifier",
                                                 "generic_type"):
                        bases.append(self._node_text(heritage_child, source))

        docstring = self._get_ts_docstring(node, source)

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

    # ── interfaces ──

    def _extract_ts_interface(self, node, source: bytes, result: ParseResult,
                              parent_symbol: Optional[Symbol],
                              extra_decorators: Optional[list[str]] = None) -> Symbol:
        name = ""
        bases = []
        decorators = list(extra_decorators) if extra_decorators else []

        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)
            elif child.type == "extends_type_clause":
                for ext_child in child.children:
                    if ext_child.type in ("identifier", "type_identifier",
                                          "generic_type", "nested_type_identifier"):
                        bases.append(self._node_text(ext_child, source))

        docstring = self._get_ts_docstring(node, source)

        # Extract method signatures from interface_body
        params_from_methods = []
        body = self._find_child(node, "interface_body") or self._find_child(node, "object_type")

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

        # Extract method signatures as child methods
        if body:
            for child in body.children:
                if child.type in ("method_signature", "property_signature"):
                    self._extract_ts_interface_member(child, source, result, sym)

        return sym

    def _extract_ts_interface_member(self, node, source: bytes, result: ParseResult,
                                     parent_symbol: Symbol):
        """Extract a method or property signature from an interface body."""
        name = ""
        params = []
        return_type = None

        for child in node.children:
            if child.type == "property_identifier":
                name = self._node_text(child, source)
            elif child.type == "call_signature":
                params, return_type = self._extract_call_signature(child, source)
            elif child.type == "type_annotation":
                return_type = self._extract_type_annotation(child, source)

        if not name:
            return

        kind = "method" if node.type == "method_signature" else "function"

        sym = Symbol(
            kind=kind,
            name=name,
            params_json=json.dumps(params),
            return_type=return_type,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)

    # ── type aliases ──

    def _extract_ts_type_alias(self, node, source: bytes, result: ParseResult,
                               parent_symbol: Optional[Symbol],
                               extra_decorators: Optional[list[str]] = None):
        name = ""
        decorators = list(extra_decorators) if extra_decorators else []

        for child in node.children:
            if child.type == "type_identifier":
                name = self._node_text(child, source)

        docstring = self._get_ts_docstring(node, source)

        sym = Symbol(
            kind="interface",
            name=name,
            decorators_json=json.dumps(decorators),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)

    # ── enums ──

    def _extract_ts_enum(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol],
                         extra_decorators: Optional[list[str]] = None) -> Symbol:
        name = ""
        decorators = list(extra_decorators) if extra_decorators else []

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)

        docstring = self._get_ts_docstring(node, source)

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

    # ── functions ──

    def _extract_ts_function(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Optional[Symbol],
                             extra_decorators: Optional[list[str]] = None) -> Symbol:
        name = ""
        params = []
        return_type = None
        decorators = list(extra_decorators) if extra_decorators else []
        is_async = any(c.type == "async" for c in node.children)

        # Collect decorators from preceding siblings
        prev = node.prev_named_sibling
        while prev and prev.type == "decorator":
            dec_text = self._node_text(prev, source).lstrip("@").strip()
            decorators.insert(0, dec_text)
            prev = prev.prev_named_sibling

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "formal_parameters":
                params = self._extract_ts_params(child, source)
            elif child.type == "type_annotation":
                return_type = self._extract_type_annotation(child, source)

        docstring = self._get_ts_docstring(node, source)
        complexity = self._compute_ts_complexity(node)

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
            is_async=is_async,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    # ── methods ──

    def _extract_ts_method(self, node, source: bytes, result: ParseResult,
                           parent_symbol: Optional[Symbol]) -> Symbol:
        name = ""
        params = []
        return_type = None
        decorators = []
        is_async = any(c.type == "async" for c in node.children)

        # Check for accessibility modifiers and static/abstract/readonly
        for child in node.children:
            if child.type == "accessibility_modifier":
                decorators.append(self._node_text(child, source))
            elif child.type == "static":
                decorators.append("static")
            elif child.type == "abstract":
                decorators.append("abstract")
            elif child.type == "readonly":
                decorators.append("readonly")
            elif child.type == "override":
                decorators.append("override")

        # Collect decorators from preceding siblings
        prev = node.prev_named_sibling
        while prev and prev.type == "decorator":
            dec_text = self._node_text(prev, source).lstrip("@").strip()
            decorators.insert(0, dec_text)
            prev = prev.prev_named_sibling

        for child in node.children:
            if child.type == "property_identifier":
                name = self._node_text(child, source)
            elif child.type == "formal_parameters":
                params = self._extract_ts_params(child, source)
            elif child.type == "type_annotation":
                return_type = self._extract_type_annotation(child, source)
            elif child.type == "computed_property_name":
                name = self._node_text(child, source)

        # getter/setter detection
        if any(c.type == "get" for c in node.children):
            decorators.append("get")
        if any(c.type == "set" for c in node.children):
            decorators.append("set")

        docstring = self._get_ts_docstring(node, source)
        complexity = self._compute_ts_complexity(node)

        sym = Symbol(
            kind="method",
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

    # ── arrow functions ──

    def _try_extract_arrow_function(self, node, source: bytes, result: ParseResult,
                                    parent_symbol: Optional[Symbol], depth: int,
                                    extra_decorators: Optional[list[str]] = None) -> bool:
        """Try to extract an arrow function from a lexical_declaration.

        Returns True if an arrow function was extracted, False otherwise.
        Pattern: const foo = (...) => { ... }
        Also matches: const foo = async (...) => { ... }
        """
        for declarator in self._find_children(node, "variable_declarator"):
            # Find the name (identifier) and value (arrow_function)
            name_node = self._find_child(declarator, "identifier")
            arrow_node = self._find_child(declarator, "arrow_function")
            if not name_node or not arrow_node:
                continue

            name = self._node_text(name_node, source)
            params = []
            return_type = None
            is_async = any(c.type == "async" for c in arrow_node.children)
            decorators = list(extra_decorators) if extra_decorators else []

            # Type annotation on the declarator (e.g., const foo: SomeType = ...)
            type_ann = self._find_child(declarator, "type_annotation")

            for child in arrow_node.children:
                if child.type == "formal_parameters":
                    params = self._extract_ts_params(child, source)
                elif child.type == "type_annotation":
                    return_type = self._extract_type_annotation(child, source)
                elif child.type == "identifier":
                    # Single param arrow: x => x + 1
                    param_name = self._node_text(child, source)
                    if param_name not in ("this",):
                        params = [{"name": param_name}]

            docstring = self._get_ts_docstring(node, source)
            complexity = self._compute_ts_complexity(arrow_node)
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
                is_async=is_async,
            )
            sym._pending_parent = parent_symbol
            result.symbols.append(sym)

            # Walk arrow function body for calls, refs, etc.
            for child in arrow_node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return True

        return False

    # ── calls ──

    def _extract_ts_call(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]):
        """Extract a call_expression: foo(), obj.method(), fn<T>()."""
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

    def _extract_ts_new(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol]):
        """Extract a new_expression: new Foo(), new ns.Bar()."""
        # The constructor target is the first non-'new' child
        callee = ""
        for child in node.children:
            if child.type != "new":
                callee = self._node_text(child, source)
                break

        if not callee:
            return

        # Strip arguments portion if captured
        if "(" in callee:
            callee = callee[:callee.index("(")]

        call = Call(
            callee_expr=callee,
            line_no=node.start_point[0] + 1,
        )
        call._pending_caller = parent_symbol
        result.calls.append(call)

    # ── refs ──

    def _extract_ts_ref(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol]):
        """Extract member_expression references: obj.prop, this.field."""
        full_text = self._node_text(node, source)
        parts = full_text.split(".")
        if len(parts) < 2:
            return

        # Track this.X patterns (equivalent to self.X in Python)
        if parts[0] == "this" and len(parts) >= 2:
            if len(parts) == 2:
                target = "this"
                name = parts[1]
            elif len(parts) >= 3:
                target = f"this.{parts[1]}"
                name = parts[2]
            else:
                return

            ref = Ref(
                ref_kind="read",
                target=target,
                name=name,
                line_no=node.start_point[0] + 1,
            )
            ref._pending_symbol = parent_symbol
            result.refs.append(ref)

    # ── parameters ──

    def _extract_ts_params(self, node, source: bytes) -> list[dict]:
        """Extract parameters from formal_parameters node."""
        params = []
        for child in node.children:
            if child.type in ("required_parameter", "optional_parameter"):
                p = self._parse_ts_param(child, source)
                if p and p.get("name") not in ("this",):
                    params.append(p)
            elif child.type == "rest_pattern":
                p = self._parse_rest_param(child, source)
                if p:
                    params.append(p)
        return params

    def _parse_ts_param(self, node, source: bytes) -> Optional[dict]:
        """Parse a single required_parameter or optional_parameter."""
        name = ""
        type_str = None
        default = None
        is_optional = node.type == "optional_parameter"

        for child in node.children:
            if child.type == "identifier" and not name:
                name = self._node_text(child, source)
            elif child.type == "type_annotation":
                type_str = self._extract_type_annotation(child, source)
            elif child.type not in ("=", ":", ",", "?", "accessibility_modifier",
                                    "readonly", "override", "identifier",
                                    "type_annotation"):
                # Likely a default value
                default = self._node_text(child, source)
            elif child.type == "accessibility_modifier":
                pass  # skip public/private/protected on constructor params
            elif child.type == "readonly":
                pass

        if not name:
            # Try destructuring pattern
            for child in node.children:
                if child.type in ("object_pattern", "array_pattern"):
                    name = self._node_text(child, source)
                    break
            if not name:
                return None

        result_dict: dict = {"name": name}
        if type_str:
            result_dict["type"] = type_str
        if default:
            result_dict["default"] = default
        if is_optional and "?" not in name:
            result_dict["name"] = name + "?"
        return result_dict

    def _parse_rest_param(self, node, source: bytes) -> Optional[dict]:
        """Parse rest parameter: ...args."""
        for child in node.children:
            if child.type == "identifier":
                name = "..." + self._node_text(child, source)
                type_str = None
                type_ann = self._find_child(node, "type_annotation")
                if type_ann:
                    type_str = self._extract_type_annotation(type_ann, source)
                result_dict: dict = {"name": name}
                if type_str:
                    result_dict["type"] = type_str
                return result_dict
        return None

    def _extract_call_signature(self, node, source: bytes) -> tuple[list[dict], Optional[str]]:
        """Extract params and return type from a call_signature node."""
        params = []
        return_type = None
        for child in node.children:
            if child.type == "formal_parameters":
                params = self._extract_ts_params(child, source)
            elif child.type == "type_annotation":
                return_type = self._extract_type_annotation(child, source)
        return params, return_type

    def _extract_type_annotation(self, node, source: bytes) -> Optional[str]:
        """Extract the type string from a type_annotation node.

        The type_annotation node typically has ':' and then the type node.
        """
        for child in node.children:
            if child.type != ":":
                return self._node_text(child, source).strip()
        return None

    # ── docstrings (JSDoc comments) ──

    def _get_ts_docstring(self, node, source: bytes) -> Optional[str]:
        """Extract JSDoc comment preceding a declaration.

        Looks for a comment node immediately before this node.
        """
        prev = node.prev_named_sibling
        if prev and prev.type == "comment":
            text = self._node_text(prev, source)
            if text.startswith("/**"):
                # Strip /** ... */ and clean up
                text = text[3:]
                if text.endswith("*/"):
                    text = text[:-2]
                lines = []
                for line in text.split("\n"):
                    line = line.strip()
                    if line.startswith("* "):
                        line = line[2:]
                    elif line.startswith("*"):
                        line = line[1:]
                    lines.append(line.strip())
                return "\n".join(lines).strip() or None
            # Single-line // comment
            if text.startswith("//"):
                return text[2:].strip() or None
        # Also check for non-named previous sibling (comment nodes may not be "named")
        if node.prev_sibling and node.prev_sibling.type == "comment":
            prev_sib = node.prev_sibling
            text = self._node_text(prev_sib, source)
            if text.startswith("/**"):
                text = text[3:]
                if text.endswith("*/"):
                    text = text[:-2]
                lines = []
                for line in text.split("\n"):
                    line = line.strip()
                    if line.startswith("* "):
                        line = line[2:]
                    elif line.startswith("*"):
                        line = line[1:]
                    lines.append(line.strip())
                return "\n".join(lines).strip() or None
        return None

    # ── complexity ──

    def _compute_ts_complexity(self, node) -> int:
        """Compute cyclomatic complexity for a function/method node."""
        complexity = 1
        for child in self._walk_all(node):
            if child.type in ("if_statement", "for_statement", "for_in_statement",
                              "while_statement", "catch_clause", "else_clause",
                              "switch_case"):
                complexity += 1
            elif child.type == "ternary_expression":
                complexity += 1
            elif child.type in ("&&", "||", "??"):
                complexity += 1
            elif child.type == "binary_expression":
                # Check operator for && || ??
                for op in child.children:
                    if op.type in ("&&", "||", "??"):
                        complexity += 1
        return complexity

    def _walk_all(self, node):
        """Recursively yield all descendant nodes."""
        for child in node.children:
            yield child
            yield from self._walk_all(child)
