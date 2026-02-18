"""
Convention checker — enforce architectural layer boundaries using config.

Checks that imports between files respect the layer rules defined in .codeindex.yaml.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

from ..config import LayerConfig, ProjectConfig
from ..store.db import Database


def check_conventions(db: Database, config: ProjectConfig) -> list[dict[str, Any]]:
    """Check all layer boundary violations.

    Returns list of violation dicts with file, import, layer, violation details.
    """
    if not config.layers:
        return []

    violations = []

    # Build layer lookup: rel_path -> layer name
    path_to_layer = _build_layer_map(db, config.layers)

    # Get all imports
    rows = db._conn.execute("""
        SELECT f.rel_path, i.module, i.name, i.line_no
        FROM imports i
        JOIN files f ON i.file_id = f.file_id
        ORDER BY f.rel_path, i.line_no
    """).fetchall()

    # Build module-to-file mapping for resolving imports
    module_to_file = _build_module_map(db)

    for row in rows:
        src_path = row["rel_path"]
        src_layer = path_to_layer.get(src_path)
        if not src_layer:
            continue

        # Resolve imported module to a file path
        import_module = row["module"]
        import_name = row["name"]

        # Try to find which layer the imported module belongs to
        target_file = module_to_file.get(import_module)
        if not target_file and import_name:
            # Try module.name form
            target_file = module_to_file.get(f"{import_module}.{import_name}")

        target_layer = path_to_layer.get(target_file) if target_file else None
        if not target_layer or target_layer == src_layer:
            continue

        # Check if this import is allowed
        layer_cfg = _get_layer_config(config.layers, src_layer)
        if layer_cfg and target_layer not in layer_cfg.allowed_imports:
            violations.append({
                "file": src_path,
                "line_no": row["line_no"],
                "import_module": import_module,
                "import_name": import_name,
                "source_layer": src_layer,
                "target_layer": target_layer,
                "message": f"Layer '{src_layer}' imports from '{target_layer}' "
                           f"(not in allowed_imports: {layer_cfg.allowed_imports})",
            })

    return violations


def _build_layer_map(db: Database, layers: list[LayerConfig]) -> dict[str, str]:
    """Map each indexed file path to its layer name."""
    files = db._conn.execute("SELECT rel_path FROM files").fetchall()
    path_to_layer: dict[str, str] = {}

    for file_row in files:
        rel_path = file_row["rel_path"]
        for layer in layers:
            for pattern in layer.paths:
                # Support glob patterns
                if fnmatch(rel_path, pattern) or rel_path.startswith(pattern.rstrip("*").rstrip("/")):
                    path_to_layer[rel_path] = layer.name
                    break

    return path_to_layer


def _build_module_map(db: Database) -> dict[str, str]:
    """Map Python module paths to file rel_paths."""
    files = db._conn.execute("SELECT rel_path FROM files").fetchall()
    result: dict[str, str] = {}

    for row in files:
        rel_path = row["rel_path"]
        # Convert file path to module path: core/state.py -> core.state
        module = rel_path.replace("/", ".").replace("\\", ".")
        if module.endswith(".py"):
            module = module[:-3]
        result[module] = rel_path

        # Also store without src/ prefix common in packages
        if module.startswith("src."):
            result[module[4:]] = rel_path

    return result


def _get_layer_config(layers: list[LayerConfig], name: str) -> LayerConfig | None:
    for layer in layers:
        if layer.name == name:
            return layer
    return None
