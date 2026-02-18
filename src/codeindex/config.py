"""
Project configuration — loads .codeindex.yaml and provides defaults.

Supports:
- project name and metadata
- ignore patterns (augments .gitignore)
- layers with allowed imports (for convention enforcement)
- seed_rules_from (instruction files to extract rules from)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class LayerConfig:
    """A named architectural layer with import constraints."""
    name: str = ""
    paths: list[str] = field(default_factory=list)  # glob patterns like "core/**"
    allowed_imports: list[str] = field(default_factory=list)  # layer names this can import from
    description: str = ""


@dataclass
class ProjectConfig:
    """Project configuration from .codeindex.yaml."""
    name: str = ""
    repo: str = ""
    instructions: str = ""  # path to CLAUDE.md or similar
    ignore: list[str] = field(default_factory=list)
    layers: list[LayerConfig] = field(default_factory=list)
    seed_rules_from: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, project_root: Path) -> "ProjectConfig":
        """Load config from .codeindex.yaml in project root, or return defaults."""
        config_path = project_root / ".codeindex.yaml"
        if not config_path.exists():
            config_path = project_root / ".codeindex.yml"
        if not config_path.exists():
            return cls()

        try:
            # Use yaml if available, otherwise parse simple format
            data = _load_yaml(config_path)
        except Exception:
            return cls()

        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> "ProjectConfig":
        project = data.get("project", {})
        layers_raw = data.get("layers", [])
        layers = []
        for layer in layers_raw:
            layers.append(LayerConfig(
                name=layer.get("name", ""),
                paths=layer.get("paths", []),
                allowed_imports=layer.get("allowed_imports", []),
                description=layer.get("description", ""),
            ))

        return cls(
            name=project.get("name", ""),
            repo=project.get("repo", ""),
            instructions=project.get("instructions", ""),
            ignore=data.get("ignore", []),
            layers=layers,
            seed_rules_from=data.get("seed_rules_from", []),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.name or self.repo or self.instructions:
            result["project"] = {}
            if self.name:
                result["project"]["name"] = self.name
            if self.repo:
                result["project"]["repo"] = self.repo
            if self.instructions:
                result["project"]["instructions"] = self.instructions
        if self.ignore:
            result["ignore"] = self.ignore
        if self.layers:
            result["layers"] = [
                {
                    "name": l.name,
                    "paths": l.paths,
                    "allowed_imports": l.allowed_imports,
                    **({"description": l.description} if l.description else {}),
                }
                for l in self.layers
            ]
        if self.seed_rules_from:
            result["seed_rules_from"] = self.seed_rules_from
        return result


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML file. Tries PyYAML first, falls back to simple parser."""
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        return _simple_yaml_parse(path)


def _simple_yaml_parse(path: Path) -> dict[str, Any]:
    """Minimal YAML-subset parser for when PyYAML is not installed.

    Handles:
    - Top-level keys with scalar or list values
    - Nested dicts (one level)
    - List items with "- value" syntax
    - List of dicts with "- key: value" syntax
    """
    text = path.read_text(encoding="utf-8")
    result: dict[str, Any] = {}
    current_key: Optional[str] = None
    current_dict: Optional[dict] = None
    current_list: Optional[list] = None
    current_list_item: Optional[dict] = None
    indent_level = 0

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        line_indent = len(line) - len(line.lstrip())

        # Top-level key
        if line_indent == 0 and ":" in stripped:
            # Flush any pending list item
            if current_list_item and current_list is not None:
                current_list.append(current_list_item)
                current_list_item = None

            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip()
            if value:
                result[key] = value
                current_key = None
                current_dict = None
                current_list = None
            else:
                current_key = key
                result[key] = {}
                current_dict = result[key]
                current_list = None
            continue

        # List item
        if stripped.startswith("- "):
            item = stripped[2:].strip()

            # Check if it's a dict item (has colon)
            if ":" in item and current_key:
                # Flush previous list item if exists
                if current_list_item and current_list is not None:
                    current_list.append(current_list_item)

                # Start new list-of-dicts
                if not isinstance(result.get(current_key), list):
                    result[current_key] = []
                    current_list = result[current_key]
                    current_dict = None

                k, _, v = item.partition(":")
                v = v.strip()
                current_list_item = {k.strip(): v}
                continue

            # Simple list item
            if current_key:
                if not isinstance(result.get(current_key), list):
                    result[current_key] = []
                    current_list = result[current_key]
                    current_dict = None
                result[current_key].append(item.strip('"').strip("'"))
            continue

        # Nested key in dict or list item
        if ":" in stripped:
            key, _, value = stripped.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")

            if current_list_item is not None:
                # Continuation of a list item dict
                if value.startswith("[") and value.endswith("]"):
                    # Inline list: [a, b, c]
                    items = [v.strip().strip('"').strip("'") for v in value[1:-1].split(",")]
                    current_list_item[key] = items
                else:
                    current_list_item[key] = value
            elif current_dict is not None:
                current_dict[key] = value

    # Flush last list item
    if current_list_item and current_list is not None:
        current_list.append(current_list_item)

    return result
