"""Lightweight validation that does not require Home Assistant to be installed."""

from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]
COMPONENT = ROOT / "custom_components" / "evohome_optimum_start"

required = {
    "__init__.py",
    "manifest.json",
    "config_flow.py",
    "controller.py",
    "const.py",
    "entity.py",
    "sensor.py",
    "binary_sensor.py",
    "switch.py",
    "button.py",
    "learning.py",
    "strings.json",
    "translations/en.json",
}
missing = sorted(path for path in required if not (COMPONENT / path).is_file())
if missing:
    raise SystemExit(f"Missing required files: {', '.join(missing)}")

trees: dict[str, ast.AST] = {}
for path in sorted(COMPONENT.glob("*.py")):
    trees[path.name] = ast.parse(
        path.read_text(encoding="utf-8"), filename=str(path)
    )

# Regression check: Python accepts calls containing **base_snapshot plus an explicit
# duplicate keyword, but raises TypeError only when that branch runs in Home Assistant.
controller_tree = trees["controller.py"]
base_snapshot_keys: set[str] = set()
for node in ast.walk(controller_tree):
    if (
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "base_snapshot"
            for target in node.targets
        )
        and isinstance(node.value, ast.Dict)
    ):
        base_snapshot_keys = {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
for node in ast.walk(controller_tree):
    if not isinstance(node, ast.Call):
        continue
    if not isinstance(node.func, ast.Attribute) or node.func.attr != "_set_snapshot":
        continue
    expands_base = any(
        keyword.arg is None
        and isinstance(keyword.value, ast.Name)
        and keyword.value.id == "base_snapshot"
        for keyword in node.keywords
    )
    duplicates = {
        keyword.arg
        for keyword in node.keywords
        if keyword.arg is not None and keyword.arg in base_snapshot_keys
    }
    if expands_base and duplicates:
        raise SystemExit(
            "Duplicate _set_snapshot values: " + ", ".join(sorted(duplicates))
        )

json_files = [
    COMPONENT / "manifest.json",
    COMPONENT / "strings.json",
    COMPONENT / "translations/en.json",
    ROOT / "hacs.json",
]
for path in json_files:
    json.loads(path.read_text(encoding="utf-8"))

strings = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
english = json.loads((COMPONENT / "translations/en.json").read_text(encoding="utf-8"))
if strings != english:
    raise SystemExit("strings.json and translations/en.json differ")

manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
if manifest.get("domain") != "evohome_optimum_start":
    raise SystemExit("Unexpected manifest domain")
if manifest.get("config_flow") is not True:
    raise SystemExit("Config flow is not enabled")

print(f"Validated {len(required)} required files, Python syntax, and JSON metadata.")

