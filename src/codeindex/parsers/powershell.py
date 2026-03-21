"""
PowerShell parser using tree-sitter.

Extracts: functions, classes, methods, calls (commands/cmdlets),
imports (Import-Module, dot-sourcing, using module), parameters, refs.
"""

from __future__ import annotations

import json
from typing import Optional

from .base import LanguageParser, ParseResult
from ..store.models import Call, Import, Ref, Symbol

# Try tree-sitter; gracefully degrade if unavailable
_TS_AVAILABLE = False
try:
    import tree_sitter_powershell as tspowershell
    from tree_sitter import Language, Parser as TSParser

    PS_LANGUAGE = Language(tspowershell.language())
    _TS_AVAILABLE = True
except Exception:
    pass


class PowerShellParser(LanguageParser):
    """PowerShell source parser using tree-sitter."""

    @property
    def language(self) -> str:
        return "powershell"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".ps1", ".psm1", ".psd1")

    def parse(self, source: str, rel_path: str) -> ParseResult:
        if not _TS_AVAILABLE:
            return ParseResult(parse_error="tree-sitter-powershell not available")
        return self._parse_tree_sitter(source, rel_path)

    # ── tree-sitter implementation ──

    def _parse_tree_sitter(self, source: str, rel_path: str) -> ParseResult:
        parser = TSParser(PS_LANGUAGE)
        tree = parser.parse(source.encode("utf-8"))
        result = ParseResult()

        if tree.root_node.has_error:
            result.parse_error = "tree-sitter parse error"

        source_bytes = source.encode("utf-8")
        self._walk_ts(tree.root_node, source_bytes, result, parent_symbol=None)
        return result

    def _walk_ts(self, node, source: bytes, result: ParseResult,
                 parent_symbol: Optional[Symbol], depth: int = 0):
        if node.type == "function_statement":
            sym = self._extract_ts_function(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "filter_statement":
            # filter is similar to function in PowerShell
            sym = self._extract_ts_function(node, source, result, parent_symbol,
                                            extra_decorators=["filter"])
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "class_statement":
            sym = self._extract_ts_class(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "class_method_definition":
            sym = self._extract_ts_method(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "class_property_definition":
            self._extract_ts_class_property(node, source, result, parent_symbol)
        elif node.type == "enum_statement":
            sym = self._extract_ts_enum(node, source, result, parent_symbol)
            for child in node.children:
                self._walk_ts(child, source, result, parent_symbol=sym, depth=depth + 1)
            return
        elif node.type == "command":
            self._extract_ts_command(node, source, result, parent_symbol)
            return  # command children handled inside
        elif node.type == "pipeline":
            self._extract_ts_pipeline(node, source, result, parent_symbol)
            return  # pipeline children handled inside
        elif node.type == "member_access":
            self._extract_ts_ref(node, source, result, parent_symbol)
        elif node.type == "invokation_expression":
            # Note: tree-sitter-powershell uses the misspelling "invokation"
            self._extract_ts_method_call(node, source, result, parent_symbol)

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

    # ── functions ──

    def _extract_ts_function(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Optional[Symbol],
                             extra_decorators: Optional[list[str]] = None) -> Symbol:
        """Extract function_statement or filter_statement.

        Tree structure:
          function_statement
            function
            function_name -> "Get-UserInfo"
            {
            script_block
              param_block
                attribute_list -> [CmdletBinding()]
                param
                (
                parameter_list
                  script_parameter -> [string]$UserName
                  ,
                  script_parameter -> [int]$MaxResults = 10
                )
              script_block_body ...
            }
        """
        name = ""
        params = []
        decorators = list(extra_decorators) if extra_decorators else []

        # Extract function name
        name_node = self._find_child(node, "function_name")
        if name_node:
            name = self._node_text(name_node, source).strip()
        else:
            # Fallback: look for simple_name or bare_word
            for child in node.children:
                if child.type in ("simple_name", "bare_word", "command_name"):
                    name = self._node_text(child, source).strip()
                    break

        # Extract parameters from script_block > param_block > parameter_list
        script_block = self._find_child(node, "script_block")
        if script_block:
            param_block = self._find_child(script_block, "param_block")
            if param_block:
                params = self._extract_params_from_block(param_block, source)
                # Collect [CmdletBinding()] and similar attributes from param_block
                self._collect_param_block_attributes(param_block, source, decorators)

        # Also check for param_block directly in function (some grammars)
        if not params:
            param_block = self._find_child(node, "param_block")
            if param_block:
                params = self._extract_params_from_block(param_block, source)
                self._collect_param_block_attributes(param_block, source, decorators)

        docstring = self._get_ts_docstring(node, source)
        complexity = self._compute_ts_complexity(node)

        kind = "method" if parent_symbol and parent_symbol.kind == "class" else "function"

        sym = Symbol(
            kind=kind,
            name=name,
            params_json=json.dumps(params),
            decorators_json=json.dumps(decorators),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
            complexity=complexity,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    # ── classes ──

    def _extract_ts_class(self, node, source: bytes, result: ParseResult,
                          parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract class_statement.

        Tree structure:
          class_statement
            simple_name -> "Logger"
            { ... }
        With inheritance:
          class_statement
            simple_name -> "Derived"
            : Base, IInterface
            { ... }
        """
        name = ""
        bases = []

        for child in node.children:
            if child.type == "simple_name" and not name:
                name = self._node_text(child, source).strip()
            elif child.type == "base_type_list":
                for base_child in child.children:
                    if base_child.type in ("simple_name", "type_spec", "type_name",
                                           "type_literal"):
                        base_text = self._node_text(base_child, source).strip()
                        if base_text and base_text not in (":", ","):
                            bases.append(base_text)

        docstring = self._get_ts_docstring(node, source)

        sym = Symbol(
            kind="class",
            name=name,
            bases_json=json.dumps(bases),
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    # ── methods ──

    def _extract_ts_method(self, node, source: bytes, result: ParseResult,
                           parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract class_method_definition.

        Tree structure:
          class_method_definition
            [class_attribute -> "static"]
            [type_literal -> "[void]"]
            simple_name -> "Write"
            (
            class_method_parameter_list
              class_method_parameter
                type_literal -> "[string]"
                variable -> "$message"
            )
            { script_block }
        """
        name = ""
        params = []
        return_type = None
        decorators = []

        for child in node.children:
            if child.type == "simple_name":
                name = self._node_text(child, source).strip()
            elif child.type == "class_method_parameter_list":
                params = self._extract_method_params(child, source)
            elif child.type == "type_literal":
                # Return type comes before the method name
                type_text = self._node_text(child, source).strip()
                # Extract type from [type] notation
                if type_text.startswith("[") and type_text.endswith("]"):
                    return_type = type_text[1:-1]
                else:
                    return_type = type_text
            elif child.type == "class_attribute":
                attr_text = self._node_text(child, source).strip().lower()
                if attr_text == "static":
                    decorators.append("static")
                elif attr_text == "hidden":
                    decorators.append("hidden")
                else:
                    decorators.append(attr_text)

        # Collect attribute decorators from preceding siblings
        self._collect_attributes(node, source, decorators)

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
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    # ── class properties ──

    def _extract_ts_class_property(self, node, source: bytes, result: ParseResult,
                                   parent_symbol: Optional[Symbol]):
        """Extract class_property_definition as a symbol reference.

        Tree structure:
          class_property_definition
            type_literal -> "[string]"
            variable -> "$LogPath"
        """
        name = ""
        type_str = None

        for child in node.children:
            if child.type == "variable":
                name = self._node_text(child, source).strip().lstrip("$")
            elif child.type == "simple_name":
                name = self._node_text(child, source).strip()
            elif child.type == "type_literal":
                type_text = self._node_text(child, source).strip()
                if type_text.startswith("[") and type_text.endswith("]"):
                    type_str = type_text[1:-1]
                else:
                    type_str = type_text

        if name and parent_symbol:
            ref = Ref(
                ref_kind="write",
                target=parent_symbol.name,
                name=name,
                line_no=node.start_point[0] + 1,
            )
            ref._pending_symbol = parent_symbol
            result.refs.append(ref)

    # ── enums ──

    def _extract_ts_enum(self, node, source: bytes, result: ParseResult,
                         parent_symbol: Optional[Symbol]) -> Symbol:
        """Extract enum_statement."""
        name = ""

        for child in node.children:
            if child.type == "simple_name":
                name = self._node_text(child, source).strip()

        docstring = self._get_ts_docstring(node, source)

        sym = Symbol(
            kind="class",
            name=name,
            docstring=docstring[:500] if docstring else None,
            line_start=node.start_point[0] + 1,
            line_end=node.end_point[0] + 1,
        )
        sym._pending_parent = parent_symbol
        result.symbols.append(sym)
        return sym

    # ── parameters ──

    def _extract_params_from_block(self, param_block, source: bytes) -> list[dict]:
        """Extract parameters from a param_block node.

        Tree structure:
          param_block
            attribute_list -> [CmdletBinding()]
            param
            (
            parameter_list
              script_parameter
                attribute_list -> [string] (type_literal inside)
                variable -> "$UserName"
              ,
              script_parameter
                attribute_list -> [int]
                variable -> "$MaxResults"
                script_parameter_default
                  = 10
            )
        """
        params = []
        param_list = self._find_child(param_block, "parameter_list")
        if param_list:
            for child in param_list.children:
                if child.type == "script_parameter":
                    p = self._parse_script_param(child, source)
                    if p and p.get("name") not in ("_",):
                        params.append(p)
        return params

    def _parse_script_param(self, node, source: bytes) -> Optional[dict]:
        """Parse a script_parameter node.

        Tree structure:
          script_parameter
            attribute_list
              attribute
                type_literal -> "[string]"
            variable -> "$UserName"
            [script_parameter_default
              = ...value...]
        """
        name = ""
        type_str = None
        default = None

        for child in node.children:
            if child.type == "variable":
                raw_name = self._node_text(child, source).strip()
                name = raw_name.lstrip("$")
            elif child.type == "attribute_list":
                # Look for type_literal inside attribute_list > attribute > type_literal
                type_str = self._extract_type_from_attribute_list(child, source)
            elif child.type == "script_parameter_default":
                # Default value: everything after '='
                default = self._extract_default_value(child, source)

        if not name:
            return None

        result_dict: dict = {"name": name}
        if type_str:
            result_dict["type"] = type_str
        if default:
            result_dict["default"] = default
        return result_dict

    def _extract_type_from_attribute_list(self, attr_list, source: bytes) -> Optional[str]:
        """Extract type from an attribute_list node.

        The attribute_list may contain type_literal nodes like [string].
        It may also contain [Parameter()] attributes which we ignore for type purposes.
        """
        for child in attr_list.children:
            if child.type == "attribute":
                type_lit = self._find_child(child, "type_literal")
                if type_lit:
                    type_text = self._node_text(type_lit, source).strip()
                    if type_text.startswith("[") and type_text.endswith("]"):
                        inner = type_text[1:-1]
                        # Skip attribute-like types (CmdletBinding, Parameter, etc.)
                        # Real types are lowercase or common .NET types
                        if not self._is_ps_attribute(inner):
                            return inner
                # Check for bare type_literal directly in attribute
                text = self._node_text(child, source).strip()
                if text.startswith("[") and text.endswith("]"):
                    inner = text[1:-1]
                    if not self._is_ps_attribute(inner):
                        return inner
        return None

    def _is_ps_attribute(self, name: str) -> bool:
        """Check if a name looks like a PowerShell attribute rather than a type."""
        # Common PowerShell attributes have parentheses or are known attribute names
        if "(" in name:
            return True
        known_attributes = {
            "CmdletBinding", "Parameter", "ValidateNotNullOrEmpty",
            "ValidateSet", "ValidateRange", "ValidateScript",
            "ValidatePattern", "ValidateLength", "ValidateCount",
            "Alias", "OutputType", "DscResource", "DscProperty",
        }
        return name in known_attributes

    def _extract_default_value(self, node, source: bytes) -> Optional[str]:
        """Extract the default value from a script_parameter_default node.

        Skips the '=' sign and returns the rest.
        """
        parts = []
        skip_equals = True
        for child in node.children:
            if skip_equals and child.type == "=":
                skip_equals = False
                continue
            if not skip_equals:
                text = self._node_text(child, source).strip()
                if text:
                    parts.append(text)
        return " ".join(parts) if parts else None

    def _extract_method_params(self, param_list, source: bytes) -> list[dict]:
        """Extract parameters from class_method_parameter_list.

        Tree structure:
          class_method_parameter_list
            class_method_parameter
              type_literal -> "[string]"
              variable -> "$path"
        """
        params = []
        for child in param_list.children:
            if child.type == "class_method_parameter":
                p = self._parse_method_param(child, source)
                if p and p.get("name") not in ("_",):
                    params.append(p)
        return params

    def _parse_method_param(self, node, source: bytes) -> Optional[dict]:
        """Parse a class_method_parameter node."""
        name = ""
        type_str = None
        default = None

        for child in node.children:
            if child.type == "variable":
                raw_name = self._node_text(child, source).strip()
                name = raw_name.lstrip("$")
            elif child.type == "type_literal":
                type_text = self._node_text(child, source).strip()
                if type_text.startswith("[") and type_text.endswith("]"):
                    type_str = type_text[1:-1]
                else:
                    type_str = type_text
            elif child.type == "type_spec":
                type_str = self._node_text(child, source).strip()
            elif child.type == "script_parameter_default":
                default = self._extract_default_value(child, source)

        if not name:
            return None

        result_dict: dict = {"name": name}
        if type_str:
            result_dict["type"] = type_str
        if default:
            result_dict["default"] = default
        return result_dict

    def _collect_param_block_attributes(self, param_block, source: bytes,
                                        decorators: list[str]):
        """Collect attributes from a param_block (e.g., [CmdletBinding()]).

        These appear as attribute_list nodes that are direct children of param_block,
        before the 'param' keyword.
        """
        for child in param_block.children:
            if child.type == "param":
                break  # Attributes come before 'param'
            if child.type == "attribute_list":
                for attr in child.children:
                    if attr.type == "attribute":
                        attr_text = self._node_text(attr, source).strip()
                        if attr_text and attr_text not in decorators:
                            decorators.append(attr_text)

    # ── commands / calls ──

    def _extract_ts_command(self, node, source: bytes, result: ParseResult,
                            parent_symbol: Optional[Symbol]):
        """Extract a command invocation.

        Tree structure:
          command
            command_name -> "Get-ADUser"
            command_elements
              command_parameter -> "-Identity"
              generic_token -> "foo"

        Or for dot-sourcing:
          command
            command_invokation_operator -> "."
            command_name_expr
              command_name -> "./helpers.ps1"

        Or for 'using module':
          command
            command_name -> "using"
            command_elements
              generic_token -> "module"
              generic_token -> "Az.Accounts"
        """
        # Check for dot-sourcing first
        invok_op = self._find_child(node, "command_invokation_operator")
        if invok_op:
            op_text = self._node_text(invok_op, source).strip()
            if op_text == ".":
                self._extract_dot_source(node, source, result)
                return
            elif op_text == "&":
                # & operator invocation — extract the target as a call
                name_expr = self._find_child(node, "command_name_expr")
                if name_expr:
                    callee = self._node_text(name_expr, source).strip()
                    call = Call(
                        callee_expr=callee,
                        line_no=node.start_point[0] + 1,
                    )
                    call._pending_caller = parent_symbol
                    result.calls.append(call)
                return

        # Get the command name
        cmd_name_node = self._find_child(node, "command_name")
        if not cmd_name_node:
            return

        cmd_name = self._node_text(cmd_name_node, source).strip()

        # Check for 'using module/namespace/assembly' pattern
        if cmd_name.lower() == "using":
            self._extract_using_command(node, source, result)
            return

        # Check for Import-Module
        if cmd_name.lower() == "import-module":
            self._extract_import_module(node, source, result)
            return

        call = Call(
            callee_expr=cmd_name,
            line_no=node.start_point[0] + 1,
        )
        call._pending_caller = parent_symbol
        result.calls.append(call)

    def _extract_ts_pipeline(self, node, source: bytes, result: ParseResult,
                             parent_symbol: Optional[Symbol]):
        """Extract pipeline: cmd1 | cmd2 | cmd3.

        Tree structure:
          pipeline
            pipeline_chain
              command -> "Get-ADUser ..."
              | (pipe)
              command -> "Select-Object ..."
              |
              command -> "Format-Table"

        Or for assignments:
          pipeline
            assignment_expression -> "$x = ..."

        Walk all children, extracting commands and recursing into other nodes.
        """
        for child in node.children:
            if child.type == "pipeline_chain":
                for sub in child.children:
                    if sub.type == "command":
                        self._extract_ts_command(sub, source, result, parent_symbol)
                    elif sub.type not in ("|",):
                        self._walk_ts(sub, source, result, parent_symbol=parent_symbol)
            elif child.type == "command":
                self._extract_ts_command(child, source, result, parent_symbol)
            elif child.type not in ("|",):
                self._walk_ts(child, source, result, parent_symbol=parent_symbol)

    def _extract_ts_method_call(self, node, source: bytes, result: ParseResult,
                                parent_symbol: Optional[Symbol]):
        """Extract method invocation: $obj.Method() or [Type]::StaticMethod().

        Tree structure (invokation_expression):
          type_literal -> "[Logger]"
          :: -> "::"
          member_name -> "new"
          argument_list -> "($path)"
        """
        callee = self._node_text(node, source).strip()
        # Strip the argument_list portion
        if "(" in callee:
            callee = callee[:callee.index("(")]

        if not callee:
            return

        call = Call(
            callee_expr=callee,
            line_no=node.start_point[0] + 1,
        )
        call._pending_caller = parent_symbol
        result.calls.append(call)

    # ── imports ──

    def _extract_import_module(self, node, source: bytes, result: ParseResult):
        """Extract Import-Module command arguments as imports.

        Pattern: Import-Module ModuleName [-Flag ...]
        Look at command_elements for the module name.
        """
        elements = self._find_child(node, "command_elements")
        if not elements:
            return

        module_name = ""
        skip_next = False
        for child in elements.children:
            if skip_next:
                skip_next = False
                continue
            if child.type == "command_parameter":
                # Next token after a flag might be its value; skip it
                skip_next = True
                continue
            if child.type == "command_argument_sep":
                continue
            if child.type == "generic_token" and not module_name:
                module_name = self._node_text(child, source).strip().strip("'\"")
                break
            # Also handle string_literal for quoted module names
            if child.type in ("string_literal", "expandable_string_literal"):
                module_name = self._node_text(child, source).strip().strip("'\"")
                break

        if module_name:
            result.imports.append(Import(
                module=module_name,
                is_from=False,
                line_no=node.start_point[0] + 1,
            ))

    def _extract_dot_source(self, node, source: bytes, result: ParseResult):
        """Extract dot-sourcing: . ./path.ps1.

        Tree structure:
          command
            command_invokation_operator -> "."
            command_name_expr
              command_name -> "./helpers.ps1"
        """
        name_expr = self._find_child(node, "command_name_expr")
        if name_expr:
            cmd_name = self._find_child(name_expr, "command_name")
            if cmd_name:
                path = self._node_text(cmd_name, source).strip().strip("'\"")
            else:
                path = self._node_text(name_expr, source).strip().strip("'\"")
        else:
            # Fallback: second child
            children = list(node.children)
            path = ""
            found_dot = False
            for child in children:
                if child.type == "command_invokation_operator":
                    found_dot = True
                    continue
                if found_dot:
                    path = self._node_text(child, source).strip().strip("'\"")
                    break

        if path:
            result.imports.append(Import(
                module=path,
                is_from=False,
                line_no=node.start_point[0] + 1,
            ))

    def _extract_using_command(self, node, source: bytes, result: ParseResult):
        """Extract 'using module/namespace/assembly' parsed as a command.

        Tree structure:
          command
            command_name -> "using"
            command_elements
              generic_token -> "module"
              generic_token -> "Az.Accounts"
        """
        elements = self._find_child(node, "command_elements")
        if not elements:
            return

        tokens = []
        for child in elements.children:
            if child.type in ("generic_token", "command_name"):
                tokens.append(self._node_text(child, source).strip())

        if len(tokens) >= 2:
            keyword = tokens[0].lower()  # module, namespace, assembly
            target = tokens[1]
            if keyword in ("module", "namespace", "assembly"):
                result.imports.append(Import(
                    module=target, is_from=False,
                    line_no=node.start_point[0] + 1,
                ))
        elif len(tokens) == 1:
            result.imports.append(Import(
                module=tokens[0], is_from=False,
                line_no=node.start_point[0] + 1,
            ))

    # ── refs ──

    def _extract_ts_ref(self, node, source: bytes, result: ParseResult,
                        parent_symbol: Optional[Symbol]):
        """Extract member access references: $this.Property, $obj.Method.

        Tree structure:
          member_access
            variable -> "$this"
            . -> "."
            member_name
              simple_name -> "LogPath"
        """
        # Get the object and member parts from children
        obj_node = None
        member_node = None
        for child in node.children:
            if child.type == "variable":
                obj_node = child
            elif child.type == "member_name":
                member_node = child

        if not obj_node or not member_node:
            return

        obj_text = self._node_text(obj_node, source).strip()
        member_text = self._node_text(member_node, source).strip()

        # Track $this.X patterns (equivalent to self.X in Python)
        if obj_text == "$this":
            ref = Ref(
                ref_kind="read",
                target="$this",
                name=member_text,
                line_no=node.start_point[0] + 1,
            )
            ref._pending_symbol = parent_symbol
            result.refs.append(ref)

    # ── attributes / decorators ──

    def _collect_attributes(self, node, source: bytes, decorators: list[str]):
        """Collect PowerShell attribute decorators from preceding siblings.

        E.g., [CmdletBinding()], [Parameter(Mandatory=$true)]
        """
        prev = node.prev_named_sibling
        while prev and prev.type in ("attribute", "attribute_list"):
            attr_text = self._node_text(prev, source).strip()
            if attr_text and attr_text not in decorators:
                decorators.insert(0, attr_text)
            prev = prev.prev_named_sibling

    # ── docstrings (comment-based help) ──

    def _get_ts_docstring(self, node, source: bytes) -> Optional[str]:
        """Extract PowerShell comment-based help preceding a declaration.

        Looks for:
          - Block comment: <# ... #>
          - Consecutive single-line comments: # ...
        """
        # Check previous named sibling
        prev = node.prev_named_sibling
        if prev and prev.type == "comment":
            text = self._node_text(prev, source)
            return self._clean_ps_comment(text)

        # Check previous (non-named) sibling for comment
        if node.prev_sibling and node.prev_sibling.type == "comment":
            text = self._node_text(node.prev_sibling, source)
            return self._clean_ps_comment(text)

        # Look for block comment <# ... #>
        if prev and prev.type == "block_comment":
            text = self._node_text(prev, source)
            return self._clean_ps_block_comment(text)

        return None

    def _clean_ps_comment(self, text: str) -> Optional[str]:
        """Clean a single-line PowerShell comment."""
        if text.startswith("<#"):
            return self._clean_ps_block_comment(text)
        if text.startswith("#"):
            cleaned = text[1:].strip()
            lines = []
            for line in cleaned.split("\n"):
                line = line.strip()
                if line.startswith("#"):
                    line = line[1:].strip()
                lines.append(line)
            return "\n".join(lines).strip() or None
        return text.strip() or None

    def _clean_ps_block_comment(self, text: str) -> Optional[str]:
        """Clean a block comment <# ... #>."""
        if text.startswith("<#"):
            text = text[2:]
        if text.endswith("#>"):
            text = text[:-2]
        lines = []
        for line in text.split("\n"):
            line = line.strip()
            lines.append(line)
        return "\n".join(lines).strip() or None

    # ── complexity ──

    def _compute_ts_complexity(self, node) -> int:
        """Compute cyclomatic complexity for a function/method node."""
        complexity = 1
        for child in self._walk_all(node):
            if child.type in ("if_statement", "elseif_clause", "foreach_statement",
                              "for_statement", "while_statement", "do_while_statement",
                              "switch_statement", "catch_clause", "trap_statement"):
                complexity += 1
            elif child.type == "ternary_expression":
                complexity += 1
            # Logical operators -and, -or
            elif child.type == "logical_expression":
                complexity += 1
        return complexity

    def _walk_all(self, node):
        """Recursively yield all descendant nodes."""
        for child in node.children:
            yield child
            yield from self._walk_all(child)
