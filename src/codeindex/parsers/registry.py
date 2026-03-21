"""
Parser registry — auto-detect language from file extension.

Parsers are loaded conditionally: if a tree-sitter grammar is not installed,
that parser is silently skipped.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Optional

from .base import LanguageParser
from .python import PythonParser

_PARSERS: list[LanguageParser] = [
    PythonParser(),
]

# Conditionally load parsers for optional languages
_OPTIONAL_PARSERS = [
    ("typescript", "TypeScriptParser"),
    ("powershell", "PowerShellParser"),
    ("c_lang", "CParser"),
    ("cpp", "CppParser"),
    ("csharp", "CSharpParser"),
    ("go", "GoParser"),
    ("rust", "RustParser"),
    ("java", "JavaParser"),
]

for _mod_name, _cls_name in _OPTIONAL_PARSERS:
    try:
        _mod = importlib.import_module(f".{_mod_name}", package=__package__)
        _cls = getattr(_mod, _cls_name)
        _parser = _cls()
        # Only add if the tree-sitter grammar is actually available
        if hasattr(_parser, '_available') and not _parser._available:
            continue
        _PARSERS.append(_parser)
    except (ImportError, AttributeError, Exception):
        pass

_EXT_MAP: dict[str, LanguageParser] = {}
for p in _PARSERS:
    for ext in p.extensions:
        _EXT_MAP[ext] = p


def get_parser(path: str | Path) -> Optional[LanguageParser]:
    """Return the appropriate parser for a file, or None if unsupported."""
    ext = Path(path).suffix.lower()
    return _EXT_MAP.get(ext)


def supported_extensions() -> set[str]:
    """Return all file extensions we can parse."""
    return set(_EXT_MAP.keys())
