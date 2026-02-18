"""
Parser registry — auto-detect language from file extension.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .base import LanguageParser
from .python import PythonParser

_PARSERS: list[LanguageParser] = [
    PythonParser(),
]

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
