"""
Python parser using tree-sitter.

Extracts: classes, functions/methods, calls, imports, attribute refs.
Falls back to stdlib ast if tree-sitter fails.
"""

from __future__ import annotations

import ast
import json
from typing import Optional

from .base import LanguageParser, ParseResult
from ..store.models import Call, Import, Ref, Symbol

# Try tree-sitter; fall back to ast-only mode
_TS_AVAILABLE = False
try:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser as TSParser

    PY_LANGUAGE = Language(tspython.language())
    _TS_AVAILABLE = True
except Exception:
    pass


class PythonParser(LanguageParser):
    """Python source parser. Uses tree-sitter when available, falls back to stdlib ast."""

    @property
    def language(self) -> str:
        return "python"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".py",)

    def parse(self, source: str, rel_path: str) -> ParseResult:
        if _TS_AVAILABLE:
            return self._parse_tree_sitter(source, rel_path)
        return self._parse_ast(source, rel_path)

    # ── tree-sitter implementation ──

    def _parse_tree_sitter(self, source: str, rel_path: str) -> ParseResult:
        parser = TSParser(PY_LANGUAGE)
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
        elif node.type == "import_from_statement":
            self._extract_ts_import_from(node, source, result)
        elif node.type == "class_definition":
            sym = self._extract_ts_class(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type in ("function_definition", "async_function_definition"):
            sym = self._extract_ts_function(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "call":
            self._extract_ts_call(node, source, result, parent_symbol)
        elif node.type == "attribute":
            self._extract_ts_ref(node, source, result, parent_symbol)

        for child in node.children:
            self._walk_ts(child, source, result, parent_symbol=parent_symbol, depth=depth + 1)

    def _node_text(self, node, source: bytes) -> str:
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _extract_ts_import(self, node, source: bytes, result: ParseResult):
        for child in node.children:
            if child.type == "dotted_name":
                module = self._node_text(child, source)
                result.imports.append(Import(
                    module=module, is_from=False, line_no=node.start_point[0] + 1,
                ))
            elif child.type == "aliased_import":
                parts = [c for c in child.children if c.type == "dotted_name"]
                alias_parts = [c for c in child.children if c.type == "identifier"]
                module = self._node_text(parts[0], source) if parts else ""
                alias = self._node_text(alias_parts[-1], source) if len(alias_parts) > 0 and child.child_count > 2 else None
                result.imports.append(Import(
                    module=module, alias=alias, is_from=False, line_no=node.start_point[0] + 1,
                ))

    def _extract_ts_import_from(self, node, source: bytes, result: ParseResult):
        module = ""
        names = []
        for child in node.children:
            if child.type in ("dotted_name", "relative_import"):
                module = self._node_text(child, source)
            elif child.type == "import_list":
                for item in child.children:
                    if item.type == "aliased_import":
                        name_node = item.children[0] if item.children else None
                        alias_node = item.children[-1] if item.child_count > 2 else None
                        name = self._node_text(name_node, source) if name_node else ""
                        alias = self._node_text(alias_node, source) if alias_node and alias_node != name_node else None
                        names.append((name, alias))
                    elif item.type in ("identifier", "dotted_name"):
                        names.append((self._node_text(item, source), None))
            elif child.type in ("identifier", "dotted_name") and child.prev_sibling and self._node_text(child.prev_sibling, source) == "import":
                names.append((self._node_text(child, source), None))

        for name, alias in names:
            result.imports.append(Import(
                module=module, name=name, alias=alias,
                is_from=True, line_no=node.start_point[0] + 1,
            ))
        if not names:
            result.imports.append(Import(
                module=module, is_from=True, line_no=node.start_point[0] + 1,
            ))

    def _extract_ts_class(self, node, source: bytes, result: ParseResult,
                          parent_symbol: Optional[Symbol]) -> Symbol:
        name = ""
        bases = []
        decorators = []

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "argument_list":
                for arg in child.children:
                    if arg.type not in (",", "(", ")"):
                        bases.append(self._node_text(arg, source))
            elif child.type == "decorator":
                dec_text = self._node_text(child, source).lstrip("@").strip()
                decorators.append(dec_text)

        # Check previous siblings for decorators
        prev = node.prev_named_sibling
        while prev and prev.type == "decorator":
            dec_text = self._node_text(prev, source).lstrip("@").strip()
            if dec_text not in decorators:
                decorators.insert(0, dec_text)
            prev = prev.prev_named_sibling

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
        # parent_id set later during indexing
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    def _extract_ts_function(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Optional[Symbol]) -> Symbol:
        name = ""
        params = []
        return_type = None
        decorators = []
        # tree-sitter-python: async funcs are function_definition with an "async" child
        is_async = any(c.type == "async" for c in node.children)

        for child in node.children:
            if child.type == "identifier":
                name = self._node_text(child, source)
            elif child.type == "parameters":
                params = self._extract_ts_params(child, source)
            elif child.type == "type":
                return_type = self._node_text(child, source)

        # Check previous siblings for decorators
        prev = node.prev_named_sibling
        while prev and prev.type == "decorator":
            dec_text = self._node_text(prev, source).lstrip("@").strip()
            decorators.insert(0, dec_text)
            prev = prev.prev_named_sibling

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

    def _extract_ts_params(self, node, source: bytes) -> list[dict]:
        params = []
        for child in node.children:
            if child.type in ("identifier", "typed_parameter", "default_parameter",
                              "typed_default_parameter", "list_splat_pattern",
                              "dictionary_splat_pattern"):
                p = self._parse_ts_param(child, source)
                if p and p.get("name") not in ("self", "cls"):
                    params.append(p)
        return params

    def _parse_ts_param(self, node, source: bytes) -> Optional[dict]:
        if node.type == "identifier":
            return {"name": self._node_text(node, source)}
        elif node.type == "typed_parameter":
            name = ""
            type_str = None
            for child in node.children:
                if child.type == "identifier":
                    name = self._node_text(child, source)
                elif child.type == "type":
                    type_str = self._node_text(child, source)
            result = {"name": name}
            if type_str:
                result["type"] = type_str
            return result
        elif node.type in ("default_parameter", "typed_default_parameter"):
            name = ""
            type_str = None
            default = None
            for child in node.children:
                if child.type == "identifier" and not name:
                    name = self._node_text(child, source)
                elif child.type == "type":
                    type_str = self._node_text(child, source)
                elif child.type not in ("=", ":", "identifier", "type"):
                    default = self._node_text(child, source)
            result = {"name": name}
            if type_str:
                result["type"] = type_str
            if default:
                result["default"] = default
            return result
        elif node.type == "list_splat_pattern":
            for child in node.children:
                if child.type == "identifier":
                    return {"name": "*" + self._node_text(child, source)}
        elif node.type == "dictionary_splat_pattern":
            for child in node.children:
                if child.type == "identifier":
                    return {"name": "**" + self._node_text(child, source)}
        return None

    def _extract_ts_call(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]):
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

        # Detect .connect(self.method_name) pattern — treat as call to method_name
        if callee.endswith(".connect"):
            args_node = node.children[1] if len(node.children) > 1 else None
            if args_node and args_node.type == "argument_list":
                for arg in args_node.children:
                    if arg.type == "attribute":
                        arg_text = self._node_text(arg, source)
                        # self.method_name or self.obj.method_name
                        if arg_text.startswith("self."):
                            parts = arg_text.split(".")
                            target_method = parts[-1]
                            connect_call = Call(
                                callee_expr=arg_text,
                                line_no=node.start_point[0] + 1,
                            )
                            connect_call._pending_caller = parent_symbol
                            result.calls.append(connect_call)

    def _extract_ts_ref(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol]):
        full_text = self._node_text(node, source)
        parts = full_text.split(".")
        if len(parts) < 2:
            return

        # Track self.X patterns
        if parts[0] == "self" and len(parts) >= 2:
            if len(parts) == 2:
                target = "self"
                name = parts[1]
            elif len(parts) >= 3:
                target = f"self.{parts[1]}"
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

    def _get_ts_docstring(self, node, source: bytes) -> Optional[str]:
        body = None
        for child in node.children:
            if child.type == "block":
                body = child
                break
        if not body or not body.children:
            return None
        first = body.children[0]
        if first.type == "expression_statement" and first.children:
            string_node = first.children[0]
            if string_node.type == "string":
                text = self._node_text(string_node, source)
                # Strip quotes
                for q in ('"""', "'''", '"', "'"):
                    if text.startswith(q) and text.endswith(q):
                        return text[len(q):-len(q)].strip()
        return None

    def _compute_ts_complexity(self, node) -> int:
        complexity = 1
        for child in self._walk_all(node):
            if child.type in ("if_statement", "for_statement", "while_statement",
                              "except_clause", "elif_clause"):
                complexity += 1
            elif child.type in ("and", "or"):
                complexity += 1
        return complexity

    def _walk_all(self, node):
        for child in node.children:
            yield child
            yield from self._walk_all(child)

    # ── stdlib ast fallback ──

    def _parse_ast(self, source: str, rel_path: str) -> ParseResult:
        result = ParseResult()
        try:
            tree = ast.parse(source, filename=rel_path)
        except SyntaxError as e:
            result.parse_error = f"Line {e.lineno}: {e.msg}"
            return result

        _set_parents(tree)
        visitor = _ASTVisitor()
        visitor.visit(tree)

        result.symbols = visitor.symbols
        result.calls = visitor.calls
        result.imports = visitor.imports
        result.refs = visitor.refs
        return result


# ── AST fallback visitor ──

class _ASTVisitor(ast.NodeVisitor):
    def __init__(self):
        self.symbols: list[Symbol] = []
        self.calls: list[Call] = []
        self.imports: list[Import] = []
        self.refs: list[Ref] = []
        self._current_class: Optional[Symbol] = None
        self._current_func: Optional[Symbol] = None
        self._class_stack: list[Optional[Symbol]] = []
        self._func_stack: list[Optional[Symbol]] = []

    def visit_Import(self, node):
        for alias in node.names:
            self.imports.append(Import(
                module=alias.name, alias=alias.asname,
                is_from=False, line_no=node.lineno,
            ))
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        if node.names:
            for alias in node.names:
                self.imports.append(Import(
                    module=module, name=alias.name, alias=alias.asname,
                    is_from=True, line_no=node.lineno,
                ))
        self.generic_visit(node)

    def visit_ClassDef(self, node):
        bases = [_unparse(b) for b in node.bases]
        decorators = [_unparse(d) for d in node.decorator_list]
        docstring = ast.get_docstring(node)

        sym = Symbol(
            kind="class",
            name=node.name,
            bases_json=json.dumps(bases),
            decorators_json=json.dumps(decorators),
            docstring=docstring[:500] if docstring else None,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
        )
        sym._pending_parent = self._current_class
        self.symbols.append(sym)

        self._class_stack.append(self._current_class)
        self._current_class = sym
        self.generic_visit(node)
        self._current_class = self._class_stack.pop()

    def visit_FunctionDef(self, node):
        self._process_func(node, is_async=False)

    def visit_AsyncFunctionDef(self, node):
        self._process_func(node, is_async=True)

    def _process_func(self, node, is_async: bool):
        params = []
        for arg in node.args.args:
            if arg.arg in ("self", "cls"):
                continue
            p: dict = {"name": arg.arg}
            if arg.annotation:
                p["type"] = _unparse(arg.annotation)
            params.append(p)

        defaults = node.args.defaults
        if defaults:
            offset = len(node.args.args) - len(defaults)
            self_offset = 1 if node.args.args and node.args.args[0].arg in ("self", "cls") else 0
            for i, default in enumerate(defaults):
                idx = offset + i - self_offset
                if 0 <= idx < len(params):
                    params[idx]["default"] = _unparse(default)

        return_type = _unparse(node.returns) if node.returns else None
        decorators = [_unparse(d) for d in node.decorator_list]
        docstring = ast.get_docstring(node)
        complexity = _cyclomatic_complexity(node)
        kind = "method" if self._current_class else "function"

        sym = Symbol(
            kind=kind,
            name=node.name,
            params_json=json.dumps(params),
            return_type=return_type,
            decorators_json=json.dumps(decorators),
            docstring=docstring[:500] if docstring else None,
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            complexity=complexity,
            is_async=is_async,
        )
        sym._pending_parent = self._current_class
        self.symbols.append(sym)

        self._func_stack.append(self._current_func)
        self._current_func = sym
        self.generic_visit(node)
        self._current_func = self._func_stack.pop()

    def visit_Call(self, node):
        callee = _unparse(node.func)
        call = Call(callee_expr=callee, line_no=node.lineno)
        call._pending_caller = self._current_func
        self.calls.append(call)

        # Detect .connect(self.method_name) — treat as call to the connected method
        if callee.endswith(".connect") and node.args:
            for arg in node.args:
                chain = _attribute_chain(arg)
                if chain and chain[0] == "self" and len(chain) >= 2:
                    connected = ".".join(chain)
                    connect_call = Call(callee_expr=connected, line_no=node.lineno)
                    connect_call._pending_caller = self._current_func
                    self.calls.append(connect_call)

        self.generic_visit(node)

    def visit_Attribute(self, node):
        chain = _attribute_chain(node)
        if chain and chain[0] == "self" and len(chain) >= 2:
            if len(chain) == 2:
                target, name = "self", chain[1]
            elif len(chain) >= 3:
                target = f"self.{chain[1]}"
                name = chain[2]
            else:
                self.generic_visit(node)
                return

            ref = Ref(ref_kind="read", target=target, name=name, line_no=node.lineno)
            ref._pending_symbol = self._current_func
            self.refs.append(ref)
        self.generic_visit(node)


def _unparse(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return "<unknown>"


def _attribute_chain(node) -> Optional[list[str]]:
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        parts.reverse()
        return parts
    return None


def _cyclomatic_complexity(node) -> int:
    complexity = 1
    for child in ast.walk(node):
        if isinstance(child, (ast.If, ast.For, ast.While, ast.ExceptHandler)):
            complexity += 1
        elif isinstance(child, ast.BoolOp):
            complexity += len(child.values) - 1
    return complexity


def _set_parents(tree):
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            child._parent = node
