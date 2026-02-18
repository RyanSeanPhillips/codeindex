"""
Base parser types and abstract interface.

All language parsers produce the same output types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..store.models import Call, Import, Ref, Symbol


@dataclass
class ParseResult:
    """Output of parsing a single file."""
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    parse_error: str | None = None


class LanguageParser(ABC):
    """Abstract base for language-specific parsers."""

    @property
    @abstractmethod
    def language(self) -> str:
        """Language identifier (e.g. 'python', 'typescript')."""

    @property
    @abstractmethod
    def extensions(self) -> tuple[str, ...]:
        """File extensions this parser handles (e.g. ('.py',))."""

    @abstractmethod
    def parse(self, source: str, rel_path: str) -> ParseResult:
        """Parse source code and extract structured information."""
